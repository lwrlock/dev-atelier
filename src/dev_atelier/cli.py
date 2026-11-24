"""Unified CLI gateway for FlowForge, CortexScope, and HexWard studios."""

from __future__ import annotations

import argparse
import importlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

APP_REGISTRY: dict[str, dict[str, Any]] = {
    "flowforge": {
        "name": "flowforge",
        "title": "FlowForge DAG Orchestrator & Replay Studio",
        "default_port": 11440,
        "cli_module": "flowforge.cli",
        "theme": "Obsidian Left-Rail + Bento UI",
    },
    "cortexscope": {
        "name": "cortexscope",
        "title": "CortexScope Local LLM Telemetry & RAPL Mastering Console",
        "default_port": 11435,
        "cli_module": "cortexscope.cli",
        "theme": "Charcoal Vinyl + Analog Radial VU Dock",
    },
    "hexward": {
        "name": "hexward",
        "title": "HexWard Modding Conflict Solver & ELF/PE Dissector",
        "default_port": 11460,
        "cli_module": "hexward.cli",
        "theme": "Outrun Synthwave Horizon Grid",
    },
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dev-atelier", description="Unified Systems, AI Telemetry & RE Workbench")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_info = sub.add_parser("info", help="List bundled workbench applications")
    p_info.add_argument("--json", action="store_true")

    p_serve = sub.add_parser("serve", help="Launch one of the bundled studio servers")
    p_serve.add_argument("app", choices=sorted(APP_REGISTRY.keys()))
    p_serve.add_argument("extra", nargs=argparse.REMAINDER)

    sub.add_parser("test", help="Run full test suites across all bundled apps")

    args = parser.parse_args(argv)
    if args.cmd == "info":
        payload = {"apps": list(APP_REGISTRY.values())}
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            for item in payload["apps"]:
                print(f"- {item['name']} (:{item['default_port']}): {item['title']} [{item['theme']}]")
        return 0

    if args.cmd == "serve":
        spec = APP_REGISTRY[args.app]
        mod = importlib.import_module(spec["cli_module"])
        forwarded = list(args.extra or ["serve"])
        if forwarded and forwarded[0] == "--":
            forwarded = forwarded[1:]
        if not forwarded:
            forwarded = ["serve", "--port", str(spec["default_port"])]
        return int(mod.main(forwarded) or 0)

    if args.cmd == "test":
        root = _repo_root()
        rc = subprocess.call(["make", "-C", str(root), "test"], stdout=sys.stdout, stderr=sys.stderr)
        return int(rc)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
