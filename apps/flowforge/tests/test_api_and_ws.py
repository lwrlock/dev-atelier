import asyncio
from contextlib import asynccontextmanager
import json
import socket
from pathlib import Path

import aiohttp
import pytest
import uvicorn
import websockets


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def live_flowforge(tmp_path: Path):
    from flowforge.app import create_app

    port = _free_port()
    app = create_app(db_path=tmp_path / "api.db", worker_count=4)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    server.install_signal_handlers = lambda: None
    task = asyncio.create_task(server.serve())
    while not server.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task


def test_api_workflows_cycles_runs_replay_chaos_dlq_burst_schedules_and_ws(tmp_path: Path):
    async def scenario():
        async with live_flowforge(tmp_path) as base:
            ws_url = base.replace("http://", "ws://") + "/ws/events"
            async with websockets.connect(ws_url) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert hello["type"] == "hello"
                assert len(hello["workers"]) == 4

                async with aiohttp.ClientSession() as sess:
                    # 1. Built-in workflows + compiled critical path
                    async with sess.get(f"{base}/api/workflows") as r:
                        assert r.status == 200
                        wfs = (await r.json())["workflows"]
                        assert len(wfs) >= 4

                    async with sess.get(f"{base}/api/workflows/order-payment-saga") as r:
                        wf = await r.json()
                        assert wf["compiled"]["makespan_ms"] > 0
                        assert len(wf["compiled"]["critical_path"]) >= 3

                    # 2. Reject cyclic workflow cleanly
                    async with sess.post(
                        f"{base}/api/workflows",
                        json={
                            "id": "bad-cyc",
                            "name": "Bad Cycle",
                            "nodes": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}],
                            "edges": [{"source": "a", "target": "b"}, {"source": "b", "target": "a"}],
                        },
                    ) as r:
                        assert r.status == 400
                        assert "cycle" in (await r.text()).lower()

                    # 3. Trigger run with idempotency_key + duplicate protection
                    async with sess.post(
                        f"{base}/api/workflows/ml-hyperparam-sweep/run",
                        json={"wait": True, "idempotency_key": "idem-1"},
                    ) as r:
                        assert r.status == 200
                        run1 = await r.json()
                        assert run1["status"] == "COMPLETED"
                        run_id = run1["run_id"]

                    async with sess.post(
                        f"{base}/api/workflows/ml-hyperparam-sweep/run",
                        json={"wait": False, "idempotency_key": "idem-1"},
                    ) as r:
                        assert r.status == 409
                        assert (await r.json())["existing_run_id"] == run_id

                    # 4. Time-Travel replay snapshot & Mermaid export
                    async with sess.get(f"{base}/api/runs/{run_id}/replay?seq=3") as r:
                        snap = await r.json()
                        assert snap["seq"] == 3

                    async with sess.get(f"{base}/api/workflows/ml-hyperparam-sweep/export?format=mermaid") as r:
                        mmd = await r.text()
                        assert "flowchart LR" in mmd

                    # 5. Chaos Saga + DLQ Replay
                    async with sess.post(
                        f"{base}/api/chaos/rules",
                        json={"rules": [{"node_id": "charge", "fail_prob": 1.0, "enabled": True}]},
                    ) as r:
                        assert r.status == 200

                    async with sess.post(
                        f"{base}/api/workflows/order-payment-saga/run",
                        json={"wait": True},
                    ) as r:
                        saga_res = await r.json()
                        assert saga_res["status"] == "COMPENSATED"

                    async with sess.get(f"{base}/api/dlq?unresolved_only=true") as r:
                        dlq = (await r.json())["items"]
                        assert len(dlq) == 1
                        dlq_id = dlq[0]["id"]

                    # Clear chaos then replay DLQ item cleanly
                    await sess.post(f"{base}/api/chaos/rules", json={"rules": []})
                    async with sess.post(f"{base}/api/dlq/{dlq_id}/replay") as r:
                        rep = await r.json()
                        assert rep["status"] == "COMPLETED"

                    # 6. Burst load benchmark & Cron schedules
                    async with sess.post(
                        f"{base}/api/benchmark/burst",
                        json={"workflow_id": "fin-lakehouse-etl", "count": 6},
                    ) as r:
                        bench = await r.json()
                        assert bench["count"] == 6
                        assert bench["runs_per_sec"] > 0

                    async with sess.post(
                        f"{base}/api/schedules",
                        json={"id": "sched-1", "workflow_id": "fin-lakehouse-etl", "cron_expr": "*/10 * * * *"},
                    ) as r:
                        assert r.status == 200
                        assert (await r.json())["next_run_at"] > 0

                # Ensure WS observed run_event & run_finished
                seen_types = set()
                for _ in range(40):
                    try:
                        m = json.loads(await asyncio.wait_for(ws.recv(), timeout=0.4))
                        seen_types.add(m.get("type"))
                        if "run_finished" in seen_types and "run_event" in seen_types:
                            break
                    except asyncio.TimeoutError:
                        break
                assert "run_event" in seen_types and "run_finished" in seen_types

    asyncio.run(scenario())
