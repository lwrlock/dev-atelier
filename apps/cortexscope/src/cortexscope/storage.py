"""SQLite WAL persistence layer for inference runs, token events, arena, and hardware samples."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    source TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_preview TEXT NOT NULL,
    prompt_text TEXT NOT NULL DEFAULT '',
    response_text TEXT NOT NULL DEFAULT '',
    thinking_text TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '{}',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    eval_tokens INTEGER NOT NULL DEFAULT 0,
    think_tokens INTEGER NOT NULL DEFAULT 0,
    answer_tokens INTEGER NOT NULL DEFAULT 0,
    load_ms REAL NOT NULL DEFAULT 0.0,
    prompt_eval_ms REAL NOT NULL DEFAULT 0.0,
    ttft_ms REAL NOT NULL DEFAULT 0.0,
    eval_ms REAL NOT NULL DEFAULT 0.0,
    total_ms REAL NOT NULL DEFAULT 0.0,
    tps REAL NOT NULL DEFAULT 0.0,
    itl_p50_ms REAL NOT NULL DEFAULT 0.0,
    itl_p95_ms REAL NOT NULL DEFAULT 0.0,
    itl_p99_ms REAL NOT NULL DEFAULT 0.0,
    itl_jitter_ms REAL NOT NULL DEFAULT 0.0,
    pkg_joules REAL,
    psys_joules REAL,
    avg_pkg_watts REAL,
    joules_per_100tok REAL,
    peak_temp_c REAL,
    peak_gt_mhz INTEGER,
    status TEXT NOT NULL,
    error_msg TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_model ON runs(model);
CREATE INDEX IF NOT EXISTS idx_runs_source ON runs(source);

CREATE TABLE IF NOT EXISTS token_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    phase TEXT NOT NULL,
    token_text TEXT NOT NULL,
    delta_ms REAL NOT NULL,
    elapsed_ms REAL NOT NULL,
    pkg_watts REAL,
    gt_mhz INTEGER,
    temp_c REAL
);

CREATE INDEX IF NOT EXISTS idx_tokens_run_seq ON token_events(run_id, seq);

CREATE TABLE IF NOT EXISTS arena_matches (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    prompt_text TEXT NOT NULL,
    winner_model TEXT,
    notes TEXT NOT NULL DEFAULT '',
    models_json TEXT NOT NULL,
    results_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arena_created ON arena_matches(created_at DESC);

CREATE TABLE IF NOT EXISTS hw_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    pkg_watts REAL,
    core_watts REAL,
    uncore_watts REAL,
    psys_watts REAL,
    temp_c REAL,
    cpu_pct REAL NOT NULL,
    ram_used_gb REAL NOT NULL,
    ram_total_gb REAL NOT NULL,
    gt0_act_mhz INTEGER,
    gt1_act_mhz INTEGER,
    gt0_throttle INTEGER,
    loaded_models_json TEXT NOT NULL DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS idx_hw_ts ON hw_samples(ts DESC);
"""


def _safe_csv_cell(val: Any) -> Any:
    if isinstance(val, str) and val and val[0] in ("=", "+", "-", "@"):
        return "'" + val
    return val


class TelemetryStore:
    """Thread-safe SQLite WAL store for CortexScope telemetry."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10.0, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def save_run(self, run: dict[str, Any], events: list[dict[str, Any]]) -> None:
        created_at = float(run.get("created_at") or time.time())
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO runs (
                    id, created_at, source, endpoint, model, prompt_preview,
                    prompt_text, response_text, thinking_text, options_json,
                    prompt_tokens, eval_tokens, think_tokens, answer_tokens,
                    load_ms, prompt_eval_ms, ttft_ms, eval_ms, total_ms, tps,
                    itl_p50_ms, itl_p95_ms, itl_p99_ms, itl_jitter_ms,
                    pkg_joules, psys_joules, avg_pkg_watts, joules_per_100tok,
                    peak_temp_c, peak_gt_mhz, status, error_msg
                ) VALUES (
                    :id, :created_at, :source, :endpoint, :model, :prompt_preview,
                    :prompt_text, :response_text, :thinking_text, :options_json,
                    :prompt_tokens, :eval_tokens, :think_tokens, :answer_tokens,
                    :load_ms, :prompt_eval_ms, :ttft_ms, :eval_ms, :total_ms, :tps,
                    :itl_p50_ms, :itl_p95_ms, :itl_p99_ms, :itl_jitter_ms,
                    :pkg_joules, :psys_joules, :avg_pkg_watts, :joules_per_100tok,
                    :peak_temp_c, :peak_gt_mhz, :status, :error_msg
                )
                """,
                {**run, "created_at": created_at},
            )
            conn.execute("DELETE FROM token_events WHERE run_id = ?", (run["id"],))
            if events:
                conn.executemany(
                    """
                    INSERT INTO token_events (
                        run_id, seq, phase, token_text, delta_ms, elapsed_ms, pkg_watts, gt_mhz, temp_c
                    ) VALUES (
                        :run_id, :seq, :phase, :token_text, :delta_ms, :elapsed_ms, :pkg_watts, :gt_mhz, :temp_c
                    )
                    """,
                    events,
                )
            conn.commit()

    def list_runs(
        self,
        *,
        limit: int = 100,
        model: str | None = None,
        source: str | None = None,
        status: str | None = None,
        q: str | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if model:
            clauses.append("model = ?")
            params.append(model)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if q:
            clauses.append("(prompt_preview LIKE ? OR model LIKE ? OR response_text LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"""
            SELECT id, created_at, source, endpoint, model, prompt_preview,
                   prompt_tokens, eval_tokens, think_tokens, answer_tokens,
                   load_ms, prompt_eval_ms, ttft_ms, eval_ms, total_ms, tps,
                   itl_p50_ms, itl_p95_ms, itl_p99_ms, itl_jitter_ms,
                   pkg_joules, psys_joules, avg_pkg_watts, joules_per_100tok,
                   peak_temp_c, peak_gt_mhz, status, error_msg
            FROM runs
            {where}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
            if row is None:
                return None
            events = conn.execute(
                """
                SELECT seq, phase, token_text, delta_ms, elapsed_ms, pkg_watts, gt_mhz, temp_c
                FROM token_events WHERE run_id = ? ORDER BY seq ASC
                """,
                (run_id,),
            ).fetchall()
            out = dict(row)
            out["events"] = [dict(e) for e in events]
            return out

    def delete_run(self, run_id: str) -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            conn.commit()
            return cur.rowcount > 0

    def count_token_events(self, run_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM token_events WHERE run_id = ?", (run_id,)
            ).fetchone()
            return int(row["cnt"]) if row else 0

    def export_run_csv(self, run_id: str) -> str | None:
        detail = self.get_run(run_id)
        if detail is None:
            return None
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["seq", "phase", "delta_ms", "elapsed_ms", "pkg_watts", "gt_mhz", "temp_c", "token_text"])
        for ev in detail["events"]:
            writer.writerow(
                [
                    ev["seq"],
                    ev["phase"],
                    ev["delta_ms"],
                    ev["elapsed_ms"],
                    ev["pkg_watts"],
                    ev["gt_mhz"],
                    ev["temp_c"],
                    _safe_csv_cell(ev["token_text"]),
                ]
            )
        return buf.getvalue()

    def get_stats(self) -> dict[str, Any]:
        with self._connect() as conn:
            by_model = conn.execute(
                """
                SELECT
                    model,
                    COUNT(*) AS runs,
                    ROUND(AVG(tps), 2) AS avg_tps,
                    ROUND(MAX(tps), 2) AS max_tps,
                    ROUND(AVG(ttft_ms), 1) AS avg_ttft_ms,
                    ROUND(AVG(itl_p95_ms), 2) AS avg_itl_p95_ms,
                    ROUND(AVG(joules_per_100tok), 2) AS avg_j_per_100tok,
                    ROUND(MAX(peak_temp_c), 1) AS max_temp_c,
                    SUM(eval_tokens) AS total_tokens
                FROM runs
                WHERE status = 'completed'
                GROUP BY model
                ORDER BY runs DESC, avg_tps DESC
                """
            ).fetchall()

            scatter = conn.execute(
                """
                SELECT id, model, tps, ttft_ms, itl_p95_ms, peak_temp_c, joules_per_100tok, eval_tokens
                FROM runs
                WHERE status = 'completed'
                ORDER BY created_at DESC
                LIMIT 200
                """
            ).fetchall()

            itl_rows = conn.execute(
                """
                SELECT delta_ms FROM token_events
                ORDER BY id DESC LIMIT 4000
                """
            ).fetchall()

        buckets = [
            {"label": "<15ms", "min": 0.0, "max": 15.0, "count": 0},
            {"label": "15-30ms", "min": 15.0, "max": 30.0, "count": 0},
            {"label": "30-60ms", "min": 30.0, "max": 60.0, "count": 0},
            {"label": "60-120ms", "min": 60.0, "max": 120.0, "count": 0},
            {"label": "120-250ms", "min": 120.0, "max": 250.0, "count": 0},
            {"label": ">250ms", "min": 250.0, "max": 1e9, "count": 0},
        ]
        for row in itl_rows:
            d = float(row["delta_ms"])
            for b in buckets:
                if b["min"] <= d < b["max"]:
                    b["count"] += 1
                    break

        return {
            "by_model": [dict(r) for r in by_model],
            "scatter": [dict(r) for r in scatter],
            "itl_histogram": [{"label": b["label"], "count": b["count"]} for b in buckets],
        }

    def save_arena_match(
        self,
        *,
        match_id: str,
        prompt_text: str,
        models: list[str],
        results: list[dict[str, Any]],
        winner_model: str | None = None,
        notes: str = "",
    ) -> None:
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO arena_matches (
                    id, created_at, prompt_text, winner_model, notes, models_json, results_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    match_id,
                    time.time(),
                    prompt_text,
                    winner_model,
                    notes,
                    json.dumps(models),
                    json.dumps(results),
                ),
            )
            conn.commit()

    def vote_arena_match(self, match_id: str, winner_model: str, notes: str = "") -> bool:
        with self._write_lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE arena_matches SET winner_model = ?, notes = ? WHERE id = ?",
                (winner_model, notes, match_id),
            )
            conn.commit()
            return cur.rowcount > 0

    def list_arena_matches(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM arena_matches ORDER BY created_at DESC LIMIT ?",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["models"] = json.loads(d.pop("models_json"))
            d["results"] = json.loads(d.pop("results_json"))
            out.append(d)
        return out

    def save_hw_sample(self, sample: dict[str, Any], max_rows: int = 3600) -> None:
        loaded = json.dumps(sample.get("loaded_models", []))
        with self._write_lock, self._connect() as conn:
            conn.execute(
                """
                INSERT INTO hw_samples (
                    ts, pkg_watts, core_watts, uncore_watts, psys_watts,
                    temp_c, cpu_pct, ram_used_gb, ram_total_gb,
                    gt0_act_mhz, gt1_act_mhz, gt0_throttle, loaded_models_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample["ts"],
                    sample.get("pkg_watts"),
                    sample.get("core_watts"),
                    sample.get("uncore_watts"),
                    sample.get("psys_watts"),
                    sample.get("temp_c"),
                    sample["cpu_pct"],
                    sample["ram_used_gb"],
                    sample["ram_total_gb"],
                    sample.get("gt0_act_mhz"),
                    sample.get("gt1_act_mhz"),
                    sample.get("gt0_throttle"),
                    loaded,
                ),
            )
            conn.execute(
                """
                DELETE FROM hw_samples
                WHERE id NOT IN (
                    SELECT id FROM hw_samples ORDER BY id DESC LIMIT ?
                )
                """,
                (max(1, int(max_rows)),),
            )
            conn.commit()

    def list_hw_samples(self, limit: int = 120) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM hw_samples ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 1000)),),
            ).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["loaded_models"] = json.loads(d.pop("loaded_models_json"))
            out.append(d)
        return out
