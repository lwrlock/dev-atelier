"""Deterministic sample workspace generator (mods, base configs, PE/ELF binaries & PBR textures)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import struct

import numpy as np
from PIL import Image


def _build_demo_pe64() -> bytes:
    dos = bytearray(0x80)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x80)
    pe_sig = b"PE\x00\x00"
    coff = struct.pack("<HHIIIHH", 0x8664, 4, 0, 0, 0, 0, 0x22)

    # .text: x86-64 prologue/epilogue + FOV patch signatures
    text_pattern = b"\x48\x8B\x45\xF8\x89\x45\xFC\xF3\x0F\x10\x05\x10\x00\x00\x00\xC3"
    sec_text = (text_pattern * 32)
    sec_rdata = (
        b"WeaponBalance_v2\x00PlayerDamageScale\x00Shader_OutrunBloom\x00"
        b"PakMountPoint=/Game/Mods/Valkyrie\x00Hook_CameraFOV_Patch\x00"
        + b"\x00" * 64
    )
    sec_rdata = sec_rdata.ljust(512, b"\x00")
    sec_reloc = (b"\x00\x10\x00\x00\x18\x00\x00\x00" * 32)
    rng = np.random.default_rng(1337)
    sec_crypt = rng.integers(0, 256, size=1024, dtype=np.uint8).tobytes()

    off1 = 0x200
    off2 = off1 + len(sec_text)
    off3 = off2 + len(sec_rdata)
    off4 = off3 + len(sec_reloc)

    def _hdr(name: bytes, sz: int, va: int, ptr: int, chars: int) -> bytes:
        return struct.pack("<8sIIIIIIHHI", name.ljust(8, b"\x00")[:8], sz, va, sz, ptr, 0, 0, 0, 0, chars)

    headers = b"".join([
        _hdr(b".text", len(sec_text), 0x1000, off1, 0x60000020),
        _hdr(b".rdata", len(sec_rdata), 0x2000, off2, 0x40000040),
        _hdr(b".reloc", len(sec_reloc), 0x3000, off3, 0x42000040),
        _hdr(b".pakcrpt", len(sec_crypt), 0x4000, off4, 0xC0000040),
    ])

    buf = bytearray(off4 + len(sec_crypt))
    buf[0:0x80] = dos
    buf[0x80:0x84] = pe_sig
    buf[0x84:0x98] = coff
    buf[0x98 : 0x98 + len(headers)] = headers
    buf[off1 : off1 + len(sec_text)] = sec_text
    buf[off2 : off2 + len(sec_rdata)] = sec_rdata
    buf[off3 : off3 + len(sec_reloc)] = sec_reloc
    buf[off4 : off4 + len(sec_crypt)] = sec_crypt
    return bytes(buf)


def _build_demo_elf64() -> bytes:
    # Build minimal valid ELF64 with .text, .rodata, .shstrtab
    shstrtab = b"\x00.text\x00.rodata\x00.shstrtab\x00"
    sec_text = (b"\x55\x48\x89\xE5\x48\x8B\x7D\xF8\x89\x45\xFC\x5D\xC3\x90\x90\x90" * 32)
    sec_rodata = (b"libhexhook_vulkan.so\x00render_pass_synthwave\x00AOB_HOOK_ACTIVE\x00").ljust(256, b"\x00")

    off_text = 0x100
    off_rodata = off_text + len(sec_text)
    off_shstr = off_rodata + len(sec_rodata)
    off_shdr = (off_shstr + len(shstrtab) + 15) & ~15

    e_shnum = 4
    ehdr = struct.pack(
        "<16sHHIQQQIHHHHHH",
        b"\x7fELF\x02\x01\x01" + b"\x00" * 9,
        3,  # ET_DYN
        0x3E,  # EM_X86_64
        1,
        off_text,
        0,
        off_shdr,
        0,
        64,
        0,
        0,
        64,
        e_shnum,
        3,
    )

    def _sh(name_off: int, stype: int, flags: int, addr: int, off: int, sz: int) -> bytes:
        return struct.pack("<IIQQQQIIQQ", name_off, stype, flags, addr, off, sz, 0, 0, 8, 0)

    sh0 = _sh(0, 0, 0, 0, 0, 0)
    sh1 = _sh(1, 1, 6, 0x1000, off_text, len(sec_text))
    sh2 = _sh(7, 1, 2, 0x2000, off_rodata, len(sec_rodata))
    sh3 = _sh(15, 3, 0, 0, off_shstr, len(shstrtab))

    total = off_shdr + e_shnum * 64
    buf = bytearray(total)
    buf[0:64] = ehdr
    buf[off_text : off_text + len(sec_text)] = sec_text
    buf[off_rodata : off_rodata + len(sec_rodata)] = sec_rodata
    buf[off_shstr : off_shstr + len(shstrtab)] = shstrtab
    buf[off_shdr : off_shdr + e_shnum * 64] = sh0 + sh1 + sh2 + sh3
    return bytes(buf)


def ensure_sample_workspace(workspace_dir: Path) -> Path:
    """Populate realistic game mods, configs, binaries, and PBR textures if absent."""
    ws = Path(workspace_dir)
    if (ws / "mods").exists() and any((ws / "mods").iterdir()):
        return ws

    base_cfg = ws / "base" / "config"
    base_scripts = ws / "base" / "scripts"
    base_cfg.mkdir(parents=True, exist_ok=True)
    base_scripts.mkdir(parents=True, exist_ok=True)

    (base_cfg / "combat_balance.ini").write_text(
        "[Combat]\nPlayerDamageMult=1.0\nEnemyHealthMult=1.0\nHeadshotCritMult=2.2\nRecoilRecovery=0.75\n\n"
        "[Stamina]\nSprintDrain=12.0\nDodgeCost=18.0\nRegenDelay=1.4\n",
        encoding="utf-8",
    )
    (base_cfg / "shader_pipeline.json").write_text(
        json.dumps(
            {
                "bloom": {"intensity": 0.45, "threshold": 0.85},
                "color_grading": {"lut": "neutral_filmic", "saturation": 1.0, "vignette": 0.15},
                "camera": {"fov_base": 78, "chromatic_aberration": 0.05},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (base_scripts / "ai_flanking.lua").write_text("-- Vanilla Squad AI Tactical Tree\nreturn { max_flank_radius = 18 }\n", encoding="utf-8")

    # Mod 1: Valkyrie Core Overhaul
    m1 = ws / "mods" / "01_valkyrie_core"
    (m1 / "config").mkdir(parents=True, exist_ok=True)
    (m1 / "scripts").mkdir(parents=True, exist_ok=True)
    (m1 / "mod.json").write_text(
        json.dumps({"id": "valkyrie_core", "name": "Valkyrie Core Overhaul", "author": "KestrelMod", "version": "2.4.1", "category": "Overhaul", "requires": [], "order": 1}, indent=2),
        encoding="utf-8",
    )
    (m1 / "config" / "combat_balance.ini").write_text(
        "[Combat]\nPlayerDamageMult=1.35\nEnemyHealthMult=1.45\nHeadshotCritMult=3.1\nRecoilRecovery=0.75\n\n"
        "[Stamina]\nSprintDrain=9.5\nDodgeCost=18.0\nRegenDelay=1.0\n",
        encoding="utf-8",
    )
    (m1 / "scripts" / "ai_flanking.lua").write_text("-- Valkyrie Aggressive Flank Override\nreturn { max_flank_radius = 32 }\n", encoding="utf-8")

    # Mod 2: Realism Ballistics
    m2 = ws / "mods" / "02_realism_ballistics"
    (m2 / "config").mkdir(parents=True, exist_ok=True)
    (m2 / "mod.json").write_text(
        json.dumps({"id": "realism_ballistics", "name": "Realism Ballistics & Penetration", "author": "ArmorPiercer", "version": "1.8.0", "category": "Combat", "requires": ["valkyrie_core"], "order": 2}, indent=2),
        encoding="utf-8",
    )
    (m2 / "config" / "combat_balance.ini").write_text(
        "[Combat]\nPlayerDamageMult=1.65\nEnemyHealthMult=1.0\nHeadshotCritMult=4.0\nRecoilRecovery=0.52\n\n"
        "[Stamina]\nSprintDrain=12.0\nDodgeCost=24.0\nRegenDelay=1.4\n",
        encoding="utf-8",
    )

    # Mod 3: Synthwave Neon ReShade Preset
    m3 = ws / "mods" / "03_synthwave_shaders"
    (m3 / "config").mkdir(parents=True, exist_ok=True)
    (m3 / "mod.json").write_text(
        json.dumps({"id": "synthwave_shaders", "name": "Outrun 84 Shader Pipeline", "author": "CyberVis", "version": "3.0.0", "category": "Visuals", "requires": [], "order": 3}, indent=2),
        encoding="utf-8",
    )
    (m3 / "config" / "shader_pipeline.json").write_text(
        json.dumps(
            {
                "bloom": {"intensity": 0.92, "threshold": 0.72},
                "color_grading": {"lut": "miami_violet_84", "saturation": 1.24, "vignette": 0.25},
                "camera": {"fov_base": 92, "chromatic_aberration": 0.14},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Mod 4: AI Suppress & Camera Patch
    m4 = ws / "mods" / "04_ai_fov_patch"
    (m4 / "config").mkdir(parents=True, exist_ok=True)
    (m4 / "scripts").mkdir(parents=True, exist_ok=True)
    (m4 / "mod.json").write_text(
        json.dumps({"id": "ai_fov_patch", "name": "Squad AI & UltraWide FOV Fix", "author": "lwrlock", "version": "1.1.0", "category": "Patch", "requires": ["valkyrie_core"], "order": 4}, indent=2),
        encoding="utf-8",
    )
    (m4 / "config" / "shader_pipeline.json").write_text(
        json.dumps(
            {
                "bloom": {"intensity": 0.45, "threshold": 0.65},
                "color_grading": {"lut": "neutral_filmic", "saturation": 1.05, "vignette": 0.15},
                "camera": {"fov_base": 104, "chromatic_aberration": 0.02},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (m4 / "scripts" / "ai_flanking.lua").write_text("-- Suppressive Flank + FOV Hook\nreturn { max_flank_radius = 28, suppress_window = 2.4 }\n", encoding="utf-8")

    # Binaries
    bin_dir = ws / "binaries"
    bin_dir.mkdir(parents=True, exist_ok=True)
    (bin_dir / "ValkyrieHook_x64.dll").write_bytes(_build_demo_pe64())
    (bin_dir / "libhexhook_vulkan.so").write_bytes(_build_demo_elf64())

    # Textures (Normal map & ORM packed map)
    tex_dir = ws / "textures"
    tex_dir.mkdir(parents=True, exist_ok=True)
    yy, xx = np.mgrid[0:256, 0:256]
    # Procedural cyber-panel normal map
    dx = np.sin(xx / 10.0) * 32.0 + ((xx % 64) < 3).astype(np.float32) * 38.0
    dy = np.cos(yy / 10.0) * 32.0 + ((yy % 64) < 3).astype(np.float32) * 38.0
    nrm = np.zeros((256, 256, 4), dtype=np.uint8)
    nrm[..., 0] = np.clip(128 + dx, 0, 255).astype(np.uint8)
    nrm[..., 1] = np.clip(138 + dy, 0, 255).astype(np.uint8)
    nrm[..., 2] = 246
    nrm[..., 3] = np.clip(180 + (xx ^ yy) % 60, 0, 255).astype(np.uint8)
    Image.fromarray(nrm, mode="RGBA").save(tex_dir / "neon_armor_nrm.png", format="PNG")

    orm = np.zeros((256, 256, 4), dtype=np.uint8)
    orm[..., 0] = np.clip(210 - ((xx % 32) < 4) * 90, 0, 255).astype(np.uint8)  # AO
    orm[..., 1] = np.clip(80 + 90 * np.sin(xx / 24.0) * np.cos(yy / 24.0), 0, 255).astype(np.uint8)  # Roughness
    orm[..., 2] = np.where((xx + yy) % 64 < 28, 235, 30).astype(np.uint8)  # Metallic stripes
    orm[..., 3] = 255
    Image.fromarray(orm, mode="RGBA").save(tex_dir / "neon_armor_orm.png", format="PNG")

    return ws
