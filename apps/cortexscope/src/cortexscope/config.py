"""Configuration and upstream SSRF-safe URL validation."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_upstream_url(url: str, *, allow_remote: bool = False) -> str:
    """Validate and normalize the upstream Ollama base URL.

    By default only loopback targets are permitted to prevent SSRF via config/UI overrides.
    """
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Upstream URL cannot be empty.")

    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported upstream URL scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("Upstream URL is missing a hostname.")

    host = parsed.hostname.lower()
    if not allow_remote and host not in LOOPBACK_HOSTS:
        try:
            addr = ipaddress.ip_address(host)
            if not addr.is_loopback:
                raise ValueError(f"Non-loopback upstream IP rejected: {host}")
        except ValueError as exc:
            raise ValueError(f"Non-loopback upstream host rejected: {host}") from exc

    if parsed.port is not None and not (1 <= parsed.port <= 65535):
        raise ValueError(f"Invalid upstream port: {parsed.port}")

    return raw.rstrip("/")


def default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "cortexscope"


@dataclass(slots=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 11435
    upstream_url: str = "http://127.0.0.1:11434"
    db_path: Path = default_data_dir() / "telemetry.db"
    sample_interval_sec: float = 1.0
    allow_remote_upstream: bool = False

    def __post_init__(self) -> None:
        self.upstream_url = validate_upstream_url(
            self.upstream_url, allow_remote=self.allow_remote_upstream
        )
        self.db_path = Path(self.db_path)

    @classmethod
    def from_env(cls) -> Settings:
        db_env = os.environ.get("CORTEXSCOPE_DB")
        return cls(
            host=os.environ.get("CORTEXSCOPE_HOST", "127.0.0.1"),
            port=int(os.environ.get("CORTEXSCOPE_PORT", "11435")),
            upstream_url=os.environ.get("CORTEXSCOPE_UPSTREAM", "http://127.0.0.1:11434"),
            db_path=Path(db_env) if db_env else default_data_dir() / "telemetry.db",
            sample_interval_sec=float(os.environ.get("CORTEXSCOPE_SAMPLE_SEC", "1.0")),
            allow_remote_upstream=os.environ.get("CORTEXSCOPE_ALLOW_REMOTE", "0") == "1",
        )
