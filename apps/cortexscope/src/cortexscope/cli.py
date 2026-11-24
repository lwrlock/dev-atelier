"""CLI entrypoint for CortexScope."""

from __future__ import annotations

import argparse
from pathlib import Path
import webbrowser

import uvicorn

from .app import create_app
from .config import Settings, default_data_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cortexscope",
        description="Local LLM Cognitive, Token Latency & Hardware Energy Telemetry HUD",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    serve_p = sub.add_parser("serve", help="Run CortexScope HTTP proxy & telemetry HUD")
    serve_p.add_argument("--host", default="127.0.0.1")
    serve_p.add_argument("--port", type=int, default=11435)
    serve_p.add_argument("--upstream", default="http://127.0.0.1:11434")
    serve_p.add_argument("--db", default=str(default_data_dir() / "telemetry.db"))
    serve_p.add_argument("--sample-sec", type=float, default=1.0)
    serve_p.add_argument("--allow-remote-upstream", action="store_true")
    serve_p.add_argument("--open", action="store_true", help="Open HUD in default browser")

    args = parser.parse_args(argv)
    if args.cmd == "serve":
        cfg = Settings(
            host=args.host,
            port=args.port,
            upstream_url=args.upstream,
            db_path=Path(args.db),
            sample_interval_sec=args.sample_sec,
            allow_remote_upstream=args.allow_remote_upstream,
        )
        if args.open:
            webbrowser.open(f"http://{cfg.host}:{cfg.port}")
        app = create_app(cfg)
        uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
