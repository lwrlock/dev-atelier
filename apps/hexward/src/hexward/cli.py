"""CLI entrypoint for HexWard."""

from __future__ import annotations

import argparse
from pathlib import Path
import webbrowser

import uvicorn

from .app import create_app, default_workspace_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hexward", description="HexWard Game Modding & Binary Dissection Studio")
    sub = parser.add_subparsers(dest="cmd", required=True)
    srv = sub.add_parser("serve", help="Run HexWard Studio server")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=11460)
    srv.add_argument("--workspace", default=str(default_workspace_dir()))
    srv.add_argument("--open", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        if args.open:
            webbrowser.open(f"http://{args.host}:{args.port}")
        app = create_app(workspace_dir=Path(args.workspace))
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
