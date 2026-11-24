"""ELF/PE Binary Dissector, Sliding-Window Shannon Entropy Profiler, AOB Wildcard Scanner & String Extractor."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from typing import Any


def shannon_entropy(data: bytes) -> float:
    """Calculate exact Shannon entropy in bits/byte over [0.0, 8.0]."""
    if not data:
        return 0.0
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    ent = 0.0
    for c in counts:
        if c:
            p = c / n
            ent -= p * math.log2(p)
    return round(ent, 4)


def compute_entropy_profile(data: bytes, window_size: int = 256) -> list[dict[str, Any]]:
    """Slice binary payload into windows and classify each region by Shannon entropy."""
    w = max(32, int(window_size))
    out: list[dict[str, Any]] = []
    for off in range(0, len(data), w):
        chunk = data[off : off + w]
        ent = shannon_entropy(chunk)
        zone = "PACKED/CRYPT" if ent >= 6.8 else ("CODE/DATA" if ent >= 4.2 else "TEXT/PAD")
        out.append({"offset": off, "size": len(chunk), "entropy": ent, "zone": zone})
    return out


def extract_ascii_strings(data: bytes, min_len: int = 5, limit: int = 120) -> list[dict[str, Any]]:
    """Extract null/printable ASCII strings with byte offsets."""
    pattern = re.compile(rb"[\x20-\x7E]{" + str(max(3, min_len)).encode() + rb",}")
    results: list[dict[str, Any]] = []
    for m in pattern.finditer(data):
        results.append({"offset": m.start(), "hex_offset": hex(m.start()), "text": m.group(0).decode("ascii", errors="replace")})
        if len(results) >= limit:
            break
    return results


def _parse_elf_sections(data: bytes) -> tuple[str, str, list[dict[str, Any]]]:
    if len(data) < 64 or data[:4] != b"\x7fELF":
        return ("RAW", "Unknown", [])
    ei_class = data[4]
    is_64 = ei_class == 2
    arch = "ELF64" if is_64 else "ELF32"
    endian = "<" if data[5] == 1 else ">"

    if is_64:
        e_machine = struct.unpack_from(endian + "H", data, 18)[0]
        e_shoff = struct.unpack_from(endian + "Q", data, 40)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(endian + "HHH", data, 58)
    else:
        e_machine = struct.unpack_from(endian + "H", data, 18)[0]
        e_shoff = struct.unpack_from(endian + "I", data, 32)[0]
        e_shentsize, e_shnum, e_shstrndx = struct.unpack_from(endian + "HHH", data, 46)

    mach_map = {0x3E: "x86-64", 0x03: "x86", 0xB7: "AArch64", 0xF3: "RISC-V"}
    machine = mach_map.get(e_machine, f"Machine-0x{e_machine:x}")

    sections: list[dict[str, Any]] = []
    if e_shoff and e_shnum and e_shentsize >= (64 if is_64 else 40) and e_shoff + e_shnum * e_shentsize <= len(data):
        str_tab = b""
        if e_shstrndx < e_shnum:
            sh_base = e_shoff + e_shstrndx * e_shentsize
            if is_64:
                st_off, st_sz = struct.unpack_from(endian + "QQ", data, sh_base + 24)
            else:
                st_off, st_sz = struct.unpack_from(endian + "II", data, sh_base + 16)
            str_tab = data[st_off : st_off + st_sz]

        for idx in range(e_shnum):
            base = e_shoff + idx * e_shentsize
            if is_64:
                sh_name, sh_type, sh_flags, sh_addr, sh_off, sh_size = struct.unpack_from(endian + "IIQQQQ", data, base)
            else:
                sh_name, sh_type, sh_flags, sh_addr, sh_off, sh_size = struct.unpack_from(endian + "IIIIII", data, base)
            end = str_tab.find(b"\x00", sh_name)
            name = str_tab[sh_name : end if end != -1 else sh_name + 24].decode("utf-8", errors="replace") if sh_name < len(str_tab) else f"sec_{idx}"
            if not name and idx == 0:
                continue
            chunk = data[sh_off : sh_off + sh_size] if sh_off + sh_size <= len(data) else b""
            sections.append(
                {
                    "name": name or f"sec_{idx}",
                    "offset": int(sh_off),
                    "virtual_addr": hex(sh_addr),
                    "size": int(sh_size),
                    "flags": hex(sh_flags),
                    "entropy": shannon_entropy(chunk),
                }
            )
    return (arch, machine, sections)


def _parse_pe_sections(data: bytes) -> tuple[str, str, list[dict[str, Any]]]:
    if len(data) < 0x40 or data[:2] != b"MZ":
        return ("RAW", "Unknown", [])
    e_lfanew = struct.unpack_from("<I", data, 0x3C)[0]
    if e_lfanew + 24 > len(data) or data[e_lfanew : e_lfanew + 4] != b"PE\x00\x00":
        return ("RAW", "Unknown", [])
    machine_val, num_secs, _, _, _, opt_size, _ = struct.unpack_from("<HHIIIHH", data, e_lfanew + 4)
    fmt = "PE64" if machine_val in (0x8664, 0xAA64) else "PE32"
    mach = {0x8664: "x86-64", 0x014C: "x86", 0xAA64: "ARM64"}.get(machine_val, f"0x{machine_val:x}")

    sec_off = e_lfanew + 24 + opt_size
    sections: list[dict[str, Any]] = []
    for i in range(num_secs):
        off = sec_off + i * 40
        if off + 40 > len(data):
            break
        raw_name, vsize, vaddr, rsize, rptr, _, _, _, _, chars = struct.unpack_from("<8sIIIIIIHHI", data, off)
        name = raw_name.split(b"\x00", 1)[0].decode("ascii", errors="replace") or f".sec{i}"
        chunk = data[rptr : rptr + rsize] if rptr + rsize <= len(data) else b""
        sections.append(
            {
                "name": name,
                "offset": int(rptr),
                "virtual_addr": hex(vaddr),
                "size": int(rsize),
                "flags": hex(chars),
                "entropy": shannon_entropy(chunk),
            }
        )
    return (fmt, mach, sections)


def dissect_binary(data: bytes, filename: str = "sample.bin") -> dict[str, Any]:
    """Dissect ELF, PE, or raw asset archive into sections, entropy curve, and strings."""
    if data[:4] == b"\x7fELF":
        fmt, arch, sections = _parse_elf_sections(data)
    elif data[:2] == b"MZ":
        fmt, arch, sections = _parse_pe_sections(data)
    else:
        fmt, arch, sections = ("ARCHIVE/RAW", "Generic", [])

    if not sections and len(data) > 0:
        chunk_sz = max(256, len(data) // 4)
        for idx, start in enumerate(range(0, len(data), chunk_sz)):
            part = data[start : start + chunk_sz]
            sections.append(
                {
                    "name": f".chunk_{idx}",
                    "offset": start,
                    "virtual_addr": hex(start),
                    "size": len(part),
                    "flags": "0x0",
                    "entropy": shannon_entropy(part),
                }
            )

    profile = compute_entropy_profile(data, window_size=max(64, len(data) // 48 or 64))
    return {
        "filename": filename,
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "format": fmt,
        "arch": arch,
        "overall_entropy": shannon_entropy(data),
        "sections": sections,
        "entropy_profile": profile,
        "strings": extract_ascii_strings(data, min_len=5, limit=80),
    }


def scan_aob_pattern(
    data: bytes,
    pattern: str,
    *,
    sections: list[dict[str, Any]] | None = None,
    limit: int = 64,
) -> list[dict[str, Any]]:
    """Scan binary buffer for hex AOB byte sequence with `??` / `?` wildcards."""
    tokens = [t.strip() for t in pattern.replace(",", " ").split() if t.strip()]
    if not tokens:
        raise ValueError("Empty AOB pattern")

    compiled: list[int | None] = []
    for tok in tokens:
        if tok in ("?", "??", "**", "xx"):
            compiled.append(None)
        else:
            compiled.append(int(tok, 16))

    plen = len(compiled)
    hits: list[dict[str, Any]] = []
    secs = sections or []

    def _find_section(offset: int) -> str:
        for s in secs:
            if s["offset"] <= offset < s["offset"] + s["size"]:
                return str(s["name"])
        return ".unknown"

    first = compiled[0]
    for i in range(0, max(0, len(data) - plen + 1)):
        if first is not None and data[i] != first:
            continue
        matched = True
        for j in range(1, plen):
            want = compiled[j]
            if want is not None and data[i + j] != want:
                matched = False
                break
        if matched:
            preview = " ".join(f"{b:02X}" for b in data[i : min(len(data), i + max(plen, 12))])
            hits.append(
                {
                    "offset": i,
                    "hex_offset": hex(i),
                    "section": _find_section(i),
                    "matched_bytes": preview,
                }
            )
            if len(hits) >= limit:
                break
    return hits


def render_hex_window(data: bytes, offset: int = 0, length: int = 256) -> dict[str, Any]:
    """Format a safe offset-bounded Hex + ASCII viewer window."""
    start = max(0, min(offset, max(0, len(data) - 1)))
    end = min(len(data), start + max(16, min(length, 1024)))
    rows: list[dict[str, str]] = []
    for off in range(start, end, 16):
        chunk = data[off : min(end, off + 16)]
        hex_str = " ".join(f"{b:02X}" for b in chunk)
        ascii_str = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        rows.append({"offset": f"0x{off:06X}", "hex": hex_str, "ascii": ascii_str})
    return {"start": start, "end": end, "total_bytes": len(data), "rows": rows}
