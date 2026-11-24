"""Strict Pydantic domain models, state enums, and SSRF validation guards."""

from __future__ import annotations

from enum import Enum
import ipaddress
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    QUEUED = "QUEUED"
    LEASED = "LEASED"
    RUNNING = "RUNNING"
    RETRYING = "RETRYING"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    COMPLETED = "COMPLETED"
    DEAD_LETTERED = "DEAD_LETTERED"


TERMINAL_TASK_STATES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.SKIPPED,
    TaskStatus.COMPENSATED,
    TaskStatus.DEAD_LETTERED,
}


class RunStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class TriggerRule(str, Enum):
    ALL_SUCCESS = "ALL_SUCCESS"
    ALL_DONE = "ALL_DONE"
    ONE_SUCCESS = "ONE_SUCCESS"
    ONE_FAILED = "ONE_FAILED"


class BreakerState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class ExecutorKind(str, Enum):
    SIM_COMPUTE = "sim_compute"
    PYTHON_BUILTIN = "python_builtin"
    HTTP_WEBHOOK = "http_webhook"
    OLLAMA_PROMPT = "ollama_prompt"
    SHELL_SAFE = "shell_safe"


LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1"}


def validate_webhook_url(url: str, *, allow_loopback: bool = False) -> str:
    """Validate outbound webhook URLs against SSRF (blocks metadata/private/file URLs)."""
    raw = (url or "").strip()
    if not raw:
        raise ValueError("Webhook URL cannot be empty.")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Unsupported webhook scheme: {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("Webhook URL missing hostname.")

    host = parsed.hostname.lower()
    if host in LOOPBACK_NAMES:
        if not allow_loopback:
            raise ValueError(f"Loopback host blocked unless explicitly allowed: {host}")
        return raw

    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_link_local or ip.is_loopback or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"Restricted IP address rejected: {host}")
    except ValueError as exc:
        if "rejected" in str(exc):
            raise
        if host.endswith(".local") or host.endswith(".internal"):
            raise ValueError(f"Internal hostname rejected: {host}")
    return raw


class TaskSpec(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    label: str = Field(min_length=1, max_length=80)
    executor: ExecutorKind = ExecutorKind.SIM_COMPUTE
    handler: str = "sim_compute"
    compensate_handler: str | None = None
    resource_group: str = "default"
    trigger_rule: TriggerRule = TriggerRule.ALL_SUCCESS
    estimated_ms: int = Field(default=120, ge=1, le=600_000)
    timeout_sec: float = Field(default=10.0, gt=0.05, le=300.0)
    max_retries: int = Field(default=2, ge=0, le=10)
    retry_base_ms: int = Field(default=100, ge=10, le=30_000)
    payload: dict[str, Any] = Field(default_factory=dict)
    x: float | None = None
    y: float | None = None


class EdgeSpec(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    target: str = Field(min_length=1, max_length=64)
    condition: str | None = None


class WorkflowSpec(BaseModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_-]+$")
    name: str = Field(min_length=1, max_length=100)
    description: str = ""
    saga_enabled: bool = True
    rate_limit_rps: float = Field(default=50.0, gt=0.1, le=5000.0)
    nodes: list[TaskSpec] = Field(min_length=1)
    edges: list[EdgeSpec] = Field(default_factory=list)

    @field_validator("nodes")
    @classmethod
    def check_unique_nodes(cls, nodes: list[TaskSpec]) -> list[TaskSpec]:
        seen = set()
        for n in nodes:
            if n.id in seen:
                raise ValueError(f"Duplicate node id: {n.id}")
            seen.add(n.id)
        return nodes


class ChaosRule(BaseModel):
    node_id: str = "*"  # '*' matches every node in the active workflow
    latency_ms: int = Field(default=0, ge=0, le=30_000)
    fail_prob: float = Field(default=0.0, ge=0.0, le=1.0)
    crash_worker_once: bool = False
    enabled: bool = True


def default_data_dir() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "flowforge"
