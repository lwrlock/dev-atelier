# Dev-Atelier Monorepo

> **Unified Systems, Local AI Telemetry & Reverse-Engineering Workbench** consolidating **FlowForge**, **CortexScope**, and **HexWard** under one workspace, CLI gateway (`dev-atelier`), CI pipeline, and shared test harness.

## Bundled Studios (`apps/`)

| App | Default Port | Theme & Focus | Screenshots |
| :--- | :--- | :--- | :--- |
| [FlowForge](apps/flowforge/README.md) | `:11440` | Deterministic DAG Workflow Engine, CPM Critical Path, Saga Compensation, Chaos Lab & Time-Travel Replay (*Obsidian Left-Rail Bento*) | [DAG](apps/flowforge/docs/screenshots/01_dag_studio.png) · [Replay](apps/flowforge/docs/screenshots/02_time_travel.png) |
| [CortexScope](apps/cortexscope/README.md) | `:11435` | Transparent Ollama LLM Proxy, ITL Token Heatmap, Linux `intel-rapl`/`xe` Energy Scope & A/B Arena (*Charcoal Analog VU Console*) | [Live Scope](apps/cortexscope/docs/screenshots/01_live_pulse.png) |
| [HexWard](apps/hexward/README.md) | `:11460` | VFS Mod Conflict Solver (Kahn DAG), 3-Way INI/JSON Patch Merger, PE64/ELF64 Entropy/AOB Dissector & PBR Channel Lab (*Outrun Synthwave*) | [VFS](apps/hexward/docs/screenshots/01_vfs_collisions.png) · [Binary](apps/hexward/docs/screenshots/02_binary_entropy_aob.png) · [PBR](apps/hexward/docs/screenshots/03_pbr_texture_lab.png) |

## Quick Start & Unified Verification

```bash
make install-apps
dev-atelier info
dev-atelier serve flowforge
dev-atelier serve cortexscope
dev-atelier serve hexward
```

Run the entire monorepo test suite (all Python `pytest` + JavaScript `node --test` suites):

```bash
make test
```
