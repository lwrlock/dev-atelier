"""Event-sourced SQLite WAL storage for workflows, runs, task attempts, events, DLQ & schedules."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator

from .models import RunStatus, TaskStatus, WorkflowSpec

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workflows (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    spec_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    idempotency_key TEXT UNIQUE,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    saga_triggered INTEGER NOT NULL DEFAULT 0,
    started_at REAL NOT NULL,
    ended_at REAL,
    duration_ms REAL,
    params_json TEXT NOT NULL DEFAULT '{}',
    chaos_json TEXT NOT NULL DEFAULT '[]',
    error_msg TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_started ON runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_wf ON runs(workflow_id);

CREATE TABLE IF NOT EXISTS task_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    phase TEXT NOT NULL DEFAULT 'exec',
    worker_id TEXT,
    status TEXT NOT NULL,
    queued_at REAL NOT NULL,
    started_at REAL,
    heartbeat_at REAL,
    ended_at REAL,
    duration_ms REAL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    stdout_text TEXT NOT NULL DEFAULT '',
    error_msg TEXT,
    chaos_injected INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_attempts_run ON task_attempts(run_id, node_id, attempt);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    node_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state_from TEXT NOT NULL,
    state_to TEXT NOT NULL,
    worker_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_events_run_seq ON run_events(run_id, seq);

CREATE TABLE IF NOT EXISTS dlq_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    workflow_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    resolved INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dlq_resolved ON dlq_items(resolved, created_at DESC);

CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
    cron_expr TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_run_at REAL,
    next_run_at REAL NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}'
);
"""


class DuplicateRunError(Exception):
    """Raised when an idempotency_key already exists in the runs table."""

    def __init__(self, existing_run_id: str) -> None:
        self.existing_run_id = existing_run_id
        super().__init__(f"Duplicate idempotency_key maps to run {existing_run_id}")


class FlowStore:
    """Thread-safe SQLite WAL repository with event-sourced replay reconstruction."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=15.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def save_workflow(self, wf: WorkflowSpec) -> None:
        now = time.time()
        spec_json = wf.model_dump_json()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO workflows (id, name, description, spec_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    spec_json = excluded.spec_json,
                    updated_at = excluded.updated_at
                """,
                (wf.id, wf.name, wf.description, spec_json, now, now),
            )
            conn.commit()

    def get_workflow(self, workflow_id: str) -> WorkflowSpec | None:
        with self._connect() as conn:
            row = conn.execute("SELECT spec_json FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        return WorkflowSpec.model_validate_json(row["spec_json"]) if row else None

    def list_workflows(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, description, spec_json, created_at, updated_at FROM workflows ORDER BY updated_at DESC"
            ).fetchall()
        out = []
        for r in rows:
            spec = json.loads(r["spec_json"])
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "description": r["description"],
                    "node_count": len(spec.get("nodes", [])),
                    "edge_count": len(spec.get("edges", [])),
                    "saga_enabled": spec.get("saga_enabled", True),
                    "spec": spec,
                    "updated_at": r["updated_at"],
                }
            )
        return out

    def delete_workflow(self, workflow_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM workflows WHERE id = ?", (workflow_id,))
            conn.commit()
            return cur.rowcount > 0

    def create_run(
        self,
        run_id: str,
        workflow_id: str,
        idempotency_key: str | None,
        trigger_type: str,
        params: dict[str, Any],
        chaos_rules: list[dict[str, Any]],
    ) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            if idempotency_key:
                existing = conn.execute(
                    "SELECT id FROM runs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if existing is not None:
                    raise DuplicateRunError(str(existing["id"]))
            try:
                conn.execute(
                    """
                    INSERT INTO runs (
                        id, workflow_id, idempotency_key, trigger_type, status,
                        started_at, params_json, chaos_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        workflow_id,
                        idempotency_key,
                        trigger_type,
                        RunStatus.RUNNING.value,
                        now,
                        json.dumps(params),
                        json.dumps(chaos_rules),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                if idempotency_key:
                    existing = conn.execute(
                        "SELECT id FROM runs WHERE idempotency_key = ?", (idempotency_key,)
                    ).fetchone()
                    if existing is not None:
                        raise DuplicateRunError(str(existing["id"])) from exc
                raise

    def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        *,
        saga_triggered: bool = False,
        duration_ms: float | None = None,
        error_msg: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE runs
                SET status = ?, saga_triggered = ?, ended_at = ?, duration_ms = ?, error_msg = ?
                WHERE id = ?
                """,
                (status.value, 1 if saga_triggered else 0, now, duration_ms, error_msg, run_id),
            )
            conn.commit()

    def list_runs(self, *, workflow_id: str | None = None, limit: int = 80) -> list[dict[str, Any]]:
        sql = """
            SELECT r.*, w.name AS workflow_name
            FROM runs r
            JOIN workflows w ON w.id = r.workflow_id
        """
        params: list[Any] = []
        if workflow_id:
            sql += " WHERE r.workflow_id = ?"
            params.append(workflow_id)
        sql += " ORDER BY r.started_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 300)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["saga_triggered"] = bool(d["saga_triggered"])
            d["params"] = json.loads(d.pop("params_json"))
            d["chaos_rules"] = json.loads(d.pop("chaos_json"))
            out.append(d)
        return out

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            r = conn.execute(
                """
                SELECT r.*, w.name AS workflow_name, w.spec_json
                FROM runs r
                JOIN workflows w ON w.id = r.workflow_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if r is None:
                return None
            attempts = conn.execute(
                "SELECT * FROM task_attempts WHERE run_id = ? ORDER BY id ASC", (run_id,)
            ).fetchall()
            events = conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY seq ASC", (run_id,)
            ).fetchall()

        d = dict(r)
        d["saga_triggered"] = bool(d["saga_triggered"])
        d["params"] = json.loads(d.pop("params_json"))
        d["chaos_rules"] = json.loads(d.pop("chaos_json"))
        d["workflow_spec"] = json.loads(d.pop("spec_json"))
        d["attempts"] = [
            {
                **dict(a),
                "chaos_injected": bool(a["chaos_injected"]),
                "input": json.loads(a["input_json"]),
                "output": json.loads(a["output_json"]),
            }
            for a in attempts
        ]
        d["events"] = [
            {**dict(e), "detail": json.loads(e["detail_json"])} for e in events
        ]
        return d

    def record_attempt_start(
        self,
        *,
        run_id: str,
        node_id: str,
        attempt: int,
        phase: str,
        worker_id: str,
        queued_at: float,
        started_at: float,
        input_data: dict[str, Any],
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO task_attempts (
                    run_id, node_id, attempt, phase, worker_id, status,
                    queued_at, started_at, heartbeat_at, input_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    node_id,
                    attempt,
                    phase,
                    worker_id,
                    TaskStatus.RUNNING.value,
                    queued_at,
                    started_at,
                    started_at,
                    json.dumps(input_data),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def finish_attempt(
        self,
        attempt_row_id: int,
        *,
        status: TaskStatus,
        ended_at: float,
        duration_ms: float,
        output_data: dict[str, Any] | None = None,
        stdout_text: str = "",
        error_msg: str | None = None,
        chaos_injected: bool = False,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE task_attempts
                SET status = ?, ended_at = ?, duration_ms = ?, output_json = ?,
                    stdout_text = ?, error_msg = ?, chaos_injected = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    ended_at,
                    round(duration_ms, 2),
                    json.dumps(output_data or {}),
                    stdout_text[:4000],
                    error_msg,
                    1 if chaos_injected else 0,
                    attempt_row_id,
                ),
            )
            conn.commit()

    def append_event(
        self,
        run_id: str,
        node_id: str,
        event_type: str,
        state_from: str,
        state_to: str,
        *,
        worker_id: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> int:
        event_ts = time.time() if ts is None else ts
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM run_events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            next_seq = int(row["max_seq"]) + 1
            conn.execute(
                """
                INSERT INTO run_events (
                    run_id, seq, ts, node_id, event_type, state_from, state_to, worker_id, detail_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    next_seq,
                    event_ts,
                    node_id,
                    event_type,
                    state_from,
                    state_to,
                    worker_id,
                    json.dumps(detail or {}),
                ),
            )
            conn.commit()
            return next_seq

    def list_events(self, run_id: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if run_id:
                rows = conn.execute(
                    "SELECT * FROM run_events WHERE run_id = ? ORDER BY seq ASC LIMIT ?",
                    (run_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM run_events ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail_json"])} for r in rows]

    def reconstruct_run_snapshot(self, run_id: str, up_to_seq: int | None = None) -> dict[str, Any] | None:
        """Deterministically rebuild exact workflow & worker state up to event seq."""
        with self._connect() as conn:
            run_row = conn.execute(
                """
                SELECT r.id, r.workflow_id, r.started_at, w.spec_json
                FROM runs r JOIN workflows w ON w.id = r.workflow_id
                WHERE r.id = ?
                """,
                (run_id,),
            ).fetchone()
            if run_row is None:
                return None
            if up_to_seq is None:
                ev_rows = conn.execute(
                    "SELECT * FROM run_events WHERE run_id = ? ORDER BY seq ASC", (run_id,)
                ).fetchall()
            else:
                ev_rows = conn.execute(
                    "SELECT * FROM run_events WHERE run_id = ? AND seq <= ? ORDER BY seq ASC",
                    (run_id, up_to_seq),
                ).fetchall()

        spec = json.loads(run_row["spec_json"])
        node_states = {n["id"]: TaskStatus.PENDING.value for n in spec.get("nodes", [])}
        attempts: dict[str, int] = {n["id"]: 0 for n in spec.get("nodes", [])}
        active_workers: dict[str, str] = {}
        last_event: dict[str, Any] | None = None

        for e in ev_rows:
            nid = e["node_id"]
            st = e["state_to"]
            wid = e["worker_id"]
            if nid in node_states and st in {s.value for s in TaskStatus}:
                node_states[nid] = st
            if e["event_type"] in {"TASK_STARTED", "SAGA_COMPENSATING"}:
                if e["event_type"] == "TASK_STARTED":
                    attempts[nid] = attempts.get(nid, 0) + 1
                if wid:
                    active_workers[wid] = nid
            elif e["event_type"] in {
                "TASK_COMPLETED",
                "TASK_FAILED",
                "TASK_RETRYING",
                "TASK_DEAD_LETTERED",
                "WORKER_CRASHED",
                "SAGA_COMPENSATED",
                "TASK_SKIPPED",
            }:
                if wid and active_workers.get(wid) == nid:
                    active_workers.pop(wid, None)
                for k, v in list(active_workers.items()):
                    if v == nid:
                        active_workers.pop(k, None)
            last_event = {**dict(e), "detail": json.loads(e["detail_json"])}

        return {
            "run_id": run_id,
            "seq": last_event["seq"] if last_event else 0,
            "elapsed_ms": round((last_event["ts"] - run_row["started_at"]) * 1000.0, 2) if last_event else 0.0,
            "node_states": node_states,
            "attempts": attempts,
            "active_workers": active_workers,
            "last_event": last_event,
        }

    def enqueue_dlq(
        self,
        run_id: str,
        workflow_id: str,
        node_id: str,
        attempt: int,
        reason: str,
        payload: dict[str, Any],
    ) -> int:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO dlq_items (run_id, workflow_id, node_id, attempt, reason, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, workflow_id, node_id, attempt, reason, json.dumps(payload), time.time()),
            )
            conn.commit()
            return int(cur.lastrowid)

    def list_dlq(self, *, unresolved_only: bool = False, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dlq_items"
        if unresolved_only:
            sql += " WHERE resolved = 0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        with self._connect() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [
            {**dict(r), "resolved": bool(r["resolved"]), "payload": json.loads(r["payload_json"])}
            for r in rows
        ]

    def mark_dlq_resolved(self, dlq_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE dlq_items SET resolved = 1 WHERE id = ?", (dlq_id,))
            conn.commit()
            return cur.rowcount > 0

    def upsert_schedule(
        self,
        schedule_id: str,
        workflow_id: str,
        cron_expr: str,
        next_run_at: float,
        enabled: bool = True,
        params: dict[str, Any] | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO schedules (id, workflow_id, cron_expr, enabled, next_run_at, params_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    cron_expr = excluded.cron_expr,
                    enabled = excluded.enabled,
                    next_run_at = excluded.next_run_at,
                    params_json = excluded.params_json
                """,
                (schedule_id, workflow_id, cron_expr, 1 if enabled else 0, next_run_at, json.dumps(params or {})),
            )
            conn.commit()

    def list_schedules(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, w.name AS workflow_name
                FROM schedules s JOIN workflows w ON w.id = s.workflow_id
                ORDER BY s.next_run_at ASC
                """
            ).fetchall()
        return [
            {**dict(r), "enabled": bool(r["enabled"]), "params": json.loads(r["params_json"])}
            for r in rows
        ]

    def update_schedule_fire(self, schedule_id: str, last_run_at: float, next_run_at: float) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE schedules SET last_run_at = ?, next_run_at = ? WHERE id = ?",
                (last_run_at, next_run_at, schedule_id),
            )
            conn.commit()

    def delete_schedule(self, schedule_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            conn.commit()
            return cur.rowcount > 0
