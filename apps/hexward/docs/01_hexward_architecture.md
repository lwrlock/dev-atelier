# HexWard Architecture & Domain Specification

> **Game Modding Conflict Solver, 3-Way Config Patch Merger, Binary Entropy/AOB Dissector & PBR Texture Inspector**

## 1. Core Subsystems
1. **Virtual File System (VFS) Load-Order Solver (`conflicts.py`)**
   - Models an ordered stack of mod archives/directories `[P0..Pn]` overwriting base game paths.
   - Detects file collisions categorized by severity (`CRITICAL` for compiled scripts/binaries, `CONFIG` for `.ini`/`.json`/`.cfg`, `ASSET` for `.png`/`.dds`/`.wav`).
   - Validates `requires` and `conflicts_with` DAG edges via Kahn's topological sort, suggesting dependency-safe load orders without mutating source files on disk.
2. **Semantic 3-Way Config/INI/JSON/Lua Patch Merger (`conflicts.py`)**
   - Given `(base_text, ours_text, theirs_text)` parses INI sections/keys or JSON paths or line hunks.
   - Non-overlapping changes merge automatically (`clean_merge_count`). Overlapping edits on the same section+key emit structured conflict entries with resolution controls (`keep_ours`, `take_theirs`, `keep_base`, `custom`), reconstructing downloadable unified output.
3. **ELF/PE Binary Dissector, Shannon Entropy & AOB Scanner (`dissector.py`)**
   - Parses real ELF32/ELF64 (`\x7fELF`) section headers (`e_shoff`, `.text`, `.rodata`, etc.) and PE32/PE32+ (`MZ` + `PE\0\0`) COFF sections.
   - Computes sliding-window **Shannon Entropy** $H(X) = -\sum p(x) \log_2 p(x)$ across `[0.0 .. 8.0]` bits/byte to expose encrypted/packed blobs (`H > 7.2`), code (`5.5..6.8`), and ASCII/padding (`< 4.5`).
   - Runs **Array-of-Bytes (AOB) wildcard scans** (`48 8B ?? ?? 89 ?? C3`) mapping hits to enclosing sections + surrounding hex/ASCII dumps + ASCII/UTF-16 string tables.
4. **PBR Texture Channel Lab (`textures.py`)**
   - Splits loaded textures into Red, Green, Blue, and Alpha grayscale PNG previews, checks power-of-two mip compliance, evaluates Normal Map unit-length variance & DirectX (`-Y`) vs OpenGL (`+Y`) orientation heuristics, and extracts per-channel histograms.

## 2. Visual Identity — Outrun Synthwave Concert Hall
- Deep violet-obsidian chassis (`#090414`) + subtle CSS 3D horizon grid (`transform: perspective(500px) rotateX(60deg)`).
- Tri-Pane Workbench + Stage Tab Bar:
  - **Stage 1: VFS Load Order & Collision Matrix** (draggable/reorderable priority rack + dominance gauges)
  - **Stage 2: 3-Way Semantic Config Merger** (Base / Mod A / Mod B / Live Patched Output)
  - **Stage 3: Binary Dissector, Entropy Horizon & AOB Scanner** (Section table + interactive Canvas Entropy Skyline + Hex/ASCII pager)
  - **Stage 4: PBR Texture Channel Lab** (Albedo / Normal / ORM R-G-B-A split inspector + DX/OGL normal check)
