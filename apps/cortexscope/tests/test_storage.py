from pathlib import Path
import threading
import pytest


def test_store_crud_cascade_and_filtering(tmp_path: Path):
    from cortexscope.storage import TelemetryStore

    db = tmp_path / "telemetry.db"
    store = TelemetryStore(db)

    run = {
        "id": "run-1",
        "source": "proxy",
        "endpoint": "/api/chat",
        "model": "qwen3.5-hizli:latest",
        "prompt_preview": "Analyze kernel scheduler latency",
        "prompt_text": "Analyze kernel scheduler latency",
        "response_text": "Scheduler looks fine.",
        "thinking_text": "Checking cfs...",
        "options_json": '{"temperature":0.2}',
        "prompt_tokens": 10,
        "eval_tokens": 3,
        "think_tokens": 1,
        "answer_tokens": 2,
        "load_ms": 5.0,
        "prompt_eval_ms": 20.0,
        "ttft_ms": 45.0,
        "eval_ms": 100.0,
        "total_ms": 150.0,
        "tps": 30.0,
        "itl_p50_ms": 32.0,
        "itl_p95_ms": 48.0,
        "itl_p99_ms": 51.0,
        "itl_jitter_ms": 6.2,
        "pkg_joules": 1.8,
        "psys_joules": 3.2,
        "avg_pkg_watts": 12.0,
        "joules_per_100tok": 60.0,
        "peak_temp_c": 66.0,
        "peak_gt_mhz": 1650,
        "status": "completed",
        "error_msg": None,
    }
    events = [
        {"run_id": "run-1", "seq": 1, "phase": "think", "token_text": "Check", "delta_ms": 45.0, "elapsed_ms": 45.0, "pkg_watts": 11.2, "gt_mhz": 1500, "temp_c": 64.0},
        {"run_id": "run-1", "seq": 2, "phase": "answer", "token_text": "OK", "delta_ms": 32.0, "elapsed_ms": 77.0, "pkg_watts": 12.8, "gt_mhz": 1650, "temp_c": 66.0},
    ]
    store.save_run(run, events)

    rows = store.list_runs(model="qwen3.5-hizli:latest", q="scheduler")
    assert len(rows) == 1
    assert rows[0]["tps"] == pytest.approx(30.0)

    detail = store.get_run("run-1")
    assert detail is not None
    assert len(detail["events"]) == 2
    assert detail["events"][0]["phase"] == "think"

    stats = store.get_stats()
    assert len(stats["by_model"]) == 1
    assert stats["by_model"][0]["model"] == "qwen3.5-hizli:latest"
    assert stats["by_model"][0]["runs"] == 1

    assert store.delete_run("run-1") is True
    assert store.get_run("run-1") is None
    # Foreign key cascade verified directly
    assert store.count_token_events("run-1") == 0


def test_store_arena_match_and_vote(tmp_path: Path):
    from cortexscope.storage import TelemetryStore

    store = TelemetryStore(tmp_path / "arena.db")
    store.save_arena_match(
        match_id="m-1",
        prompt_text="Write a Fibonacci helper.",
        models=["qwen3.5-hizli:latest", "local-scout:latest"],
        results=[{"model": "qwen3.5-hizli:latest", "tps": 22.0}],
    )
    assert store.vote_arena_match("m-1", "qwen3.5-hizli:latest", "Cleaner output") is True
    matches = store.list_arena_matches()
    assert len(matches) == 1
    assert matches[0]["winner_model"] == "qwen3.5-hizli:latest"


def test_store_wal_concurrent_writes_and_hw_pruning(tmp_path: Path):
    from cortexscope.storage import TelemetryStore

    store = TelemetryStore(tmp_path / "wal.db")
    errors: list[Exception] = []

    def worker(idx: int):
        try:
            for j in range(10):
                store.save_hw_sample(
                    {
                        "ts": float(idx * 100 + j),
                        "pkg_watts": 10.0 + j,
                        "core_watts": 5.0,
                        "uncore_watts": 2.0,
                        "psys_watts": 16.0,
                        "temp_c": 60.0,
                        "cpu_pct": 25.0,
                        "ram_used_gb": 12.0,
                        "ram_total_gb": 32.0,
                        "gt0_act_mhz": 1200,
                        "gt1_act_mhz": 900,
                        "gt0_throttle": 0,
                        "loaded_models": [],
                    },
                    max_rows=25,
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    recent = store.list_hw_samples(limit=50)
    assert 1 <= len(recent) <= 25
