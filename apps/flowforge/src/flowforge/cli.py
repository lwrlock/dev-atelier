"""CLI entrypoint for FlowForge."""

from __future__ import annotations

import argparse
from pathlib import Path
import webbrowser

import uvicorn

from .app import create_app
from .models import default_data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flowforge",
        description="Deterministic DAG Workflow Engine, Chaos Engineering & Time-Travel Observatory",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve_p = sub.add_parser("serve", help="Run FlowForge server and Observatory UI")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=11440)
    serve_p.add_argument("--workers", type=int, default=6)
    serve_p.add_argument("--db", default=str(default_data_dir() / "flowforge.db"))
    serve_p.add_argument("--open", action="store_true", help="Open Observatory UI in browser")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        if args.open:
            webbrowser.open(f"http://{args.host}:{args.port}")
        app = create_app(db_path=Path(args.db), worker_count=args.workers)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
