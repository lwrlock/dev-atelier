"""TDD tests for ELF/PE binary dissector, Shannon entropy profiler, AOB wildcard scanner & PBR texture inspector."""

import io
import os
import struct
from pathlib import Path
from PIL import Image
import numpy as np

from hexward.dissector import compute_entropy_profile, dissect_binary, scan_aob_pattern
from hexward.textures import inspect_texture_bytes


def _build_minimal_pe64() -> bytes:
    # Minimal valid-enough DOS header + PE\0\0 + COFF + 2 sections (.text, .crypt)
    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)

    pe_sig = b"PE\x00\x00"
    coff = struct.pack("<HHIIIHH", 0x8664, 2, 0, 0, 0, 0, 0x22)
    sec1_data = (b"\x48\x8b\x45\xf8\x89\x45\xfc\xc3" * 32)  # 256B repetitive code + AOB pattern
    sec2_data = bytes((i * 73 + 19) & 0xFF for i in range(256))  # pseudo-random high-entropy block
    # Make section2 truly near-max entropy using deterministic numpy RNG
    rng = np.random.default_rng(42)
    sec2_data = rng.integers(0, 256, size=512, dtype=np.uint8).tobytes()

    off1 = 0x200
    off2 = off1 + len(sec1_data)
    sh1 = struct.pack("<8sIIIIIIHHI", b".text\x00\x00\x00", len(sec1_data), 0x1000, len(sec1_data), off1, 0, 0, 0, 0, 0x60000020)
    sh2 = struct.pack("<8sIIIIIIHHI", b".pakcrpt", len(sec2_data), 0x2000, len(sec2_data), off2, 0, 0, 0, 0, 0x40000040)

    buf = bytearray(off2 + len(sec2_data))
    buf[0:0x80] = dos
    buf[0x80:0x84] = pe_sig
    buf[0x84:0x98] = coff
    buf[0x98:0x98 + 40] = sh1
    buf[0x98 + 40:0x98 + 80] = sh2
    buf[off1:off1 + len(sec1_data)] = sec1_data
    buf[off2:off2 + len(sec2_data)] = sec2_data
    return bytes(buf)


def test_dissect_pe_binary_sections_entropy_and_aob():
    blob = _build_minimal_pe64()
    info = dissect_binary(blob, filename="game_patch.dll")
    assert info["format"] == "PE64"
    assert len(info["sections"]) == 2
    assert info["sections"][0]["name"] == ".text"
    assert info["sections"][1]["name"] == ".pakcrpt"
    assert info["sections"][1]["entropy"] > 6.5

    profile = compute_entropy_profile(blob, window_size=128)
    assert len(profile) >= 4
    assert max(p["entropy"] for p in profile) > 6.5

    hits = scan_aob_pattern(blob, "48 8B ?? ?? 89 ?? FC C3", sections=info["sections"])
    assert len(hits) == 32
    assert hits[0]["section"] == ".text"
    assert hits[0]["offset"] == 0x200


def test_inspect_texture_normal_map_and_channel_previews():
    # Create an OpenGL +Y tangent-space normal map (mostly RGB ~ (128, 128, 255))
    arr = np.zeros((64, 64, 4), dtype=np.uint8)
    arr[..., 0] = 132
    arr[..., 1] = 148
    arr[..., 2] = 250
    arr[..., 3] = 220
    buf = io.BytesIO()
    Image.fromarray(arr, mode="RGBA").save(buf, format="PNG")

    meta = inspect_texture_bytes(buf.getvalue(), filename="armor_nrm.png")
    assert meta["width"] == 64
    assert meta["height"] == 64
    assert meta["is_power_of_two"] is True
    assert len(meta["channels"]) == 4
    assert meta["channels"]["B"]["mean"] > 240
    assert meta["previews"]["R"].startswith("data:image/png;base64,")
    assert meta["normal_diag"]["likely_tangent_space"] is True
