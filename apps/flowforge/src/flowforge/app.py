"""FastAPI application, WebSocket telemetry, Cron scheduler loop, and REST API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import time
from typing import Any
import uuid

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .cron import next_cron_run, parse_cron
from .engine import WorkflowEngine
from .graph import CompiledDAG, CycleError, compile_dag
from .models import ChaosRule, WorkflowSpec, default_data_dir
from .storage import DuplicateRunError, FlowStore


class RunRequest(BaseModel):
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    wait: bool = False


class ChaosRulesReq(BaseModel):
    rules: list[ChaosRule] = Field(default_factory=list)


class BurstReq(BaseModel):
    workflow_id: str
    count: int = Field(default=12, ge=1, le=50)


class ScheduleReq(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:8], min_length=1, max_length=64)
    workflow_id: str
    cron_expr: str
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


def _serialize_compiled(c: CompiledDAG) -> dict[str, Any]:
    return {
        "topological_order": c.topological_order,
        "layers": c.layers,
        "positions": {nid: {"x": xy[0], "y": xy[1]} for nid, xy in c.positions.items()},
        "cpm": {nid: asdict(m) for nid, m in c.cpm.items()},
        "critical_path": c.critical_path,
        "makespan_ms": c.makespan_ms,
    }


def _to_mermaid(wf: WorkflowSpec) -> str:
    lines = ["flowchart LR"]
    for n in wf.nodes:
        safe_label = n.label.replace('"', "'")
        lines.append(f'    {n.id}["{safe_label} ({n.estimated_ms}ms)"]')
    for e in wf.edges:
        if e.condition:
            safe_cond = e.condition.replace('"', "'")
            lines.append(f'    {e.source} -->|"{safe_cond}"| {e.target}')
        else:
            lines.append(f"    {e.source} --> {e.target}")
    return "\n".join(lines) + "\n"


def _static_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "static"


def create_app(*, db_path: Path | str | None = None, worker_count: int = 6) -> FastAPI:
    store = FlowStore(db_path or (default_data_dir() / "flowforge.db"))
    engine = WorkflowEngine(store=store, worker_count=worker_count)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await engine.start()
        stop_ev = asyncio.Event()

        async def cron_loop() -> None:
            while not stop_ev.is_set():
                try:
                    now = time.time()
                    for sch in store.list_schedules():
                        if sch["enabled"] and sch["next_run_at"] <= now:
                            try:
                                spec = parse_cron(sch["cron_expr"])
                                nxt = next_cron_run(spec, after=datetime.now(timezone.utc)).timestamp()
                                store.update_schedule_fire(sch["id"], now, nxt)
                                await engine.run_workflow(
                                    sch["workflow_id"],
                                    params=sch["params"],
                                    trigger_type=f"cron:{sch['id']}",
                                    wait=False,
                                )
                            except Exception:
                                pass
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_ev.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    continue

        cron_task = asyncio.create_task(cron_loop())
        try:
            yield
        finally:
            stop_ev.set()
            cron_task.cancel()
            try:
                await cron_task
            except asyncio.CancelledError:
                pass
            await engine.stop()

    app = FastAPI(title="FlowForge", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine
    app.state.store = store

    sdir = _static_dir()
    if sdir.exists():
        app.mount("/static", StaticFiles(directory=str(sdir)), name="static")

    @app.get("/")
    async def index() -> Response:
        idx = sdir / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"service": "FlowForge", "status": "ok"})

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "workers": len(engine.workers),
            "completed_tasks_total": engine.completed_tasks_total,
            "crashes_recovered_total": engine.crashes_recovered_total,
        }

    @app.get("/api/workflows")
    async def list_workflows() -> dict[str, Any]:
        items = store.list_workflows()
        for item in items:
            wf = WorkflowSpec.model_validate(item["spec"])
            item["compiled"] = _serialize_compiled(compile_dag(wf))
        return {"workflows": items}

    @app.get("/api/workflows/{workflow_id}")
    async def get_workflow(workflow_id: str) -> dict[str, Any]:
        wf = store.get_workflow(workflow_id)
        if wf is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        return {"workflow": wf.model_dump(), "compiled": _serialize_compiled(compile_dag(wf))}

    @app.post("/api/workflows")
    async def upsert_workflow(wf: WorkflowSpec) -> dict[str, Any]:
        try:
            compiled = compile_dag(wf)
        except (CycleError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.save_workflow(wf)
        return {"ok": True, "workflow": wf.model_dump(), "compiled": _serialize_compiled(compiled)}

    @app.delete("/api/workflows/{workflow_id}")
    async def delete_workflow(workflow_id: str) -> dict[str, Any]:
        if not store.delete_workflow(workflow_id):
            raise HTTPException(status_code=404, detail="Workflow not found")
        return {"ok": True}

    @app.get("/api/workflows/{workflow_id}/export")
    async def export_workflow(workflow_id: str, format: str = Query(default="json")) -> Response:
        wf = store.get_workflow(workflow_id)
        if wf is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        if format == "mermaid":
            return PlainTextResponse(_to_mermaid(wf), media_type="text/plain")
        return JSONResponse(wf.model_dump())

    @app.post("/api/workflows/{workflow_id}/run")
    async def trigger_run(workflow_id: str, req: RunRequest) -> Response:
        try:
            res = await engine.run_workflow(
                workflow_id,
                params=req.params,
                idempotency_key=req.idempotency_key,
                trigger_type="api",
                wait=req.wait,
            )
            return JSONResponse(res)
        except DuplicateRunError as dup:
            return JSONResponse(
                {"error": "duplicate idempotency_key", "existing_run_id": dup.existing_run_id},
                status_code=409,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except (CycleError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/runs")
    async def list_runs(workflow_id: str | None = None, limit: int = Query(default=60, ge=1, le=300)) -> dict[str, Any]:
        return {"runs": store.list_runs(workflow_id=workflow_id, limit=limit)}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        detail = store.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        wf = WorkflowSpec.model_validate(detail["workflow_spec"])
        detail["compiled"] = _serialize_compiled(compile_dag(wf))
        return detail

    @app.get("/api/runs/{run_id}/replay")
    async def replay_run(run_id: str, seq: int | None = Query(default=None, ge=0)) -> dict[str, Any]:
        snap = store.reconstruct_run_snapshot(run_id, up_to_seq=seq)
        if snap is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return snap

    @app.post("/api/runs/{run_id}/cancel")
    async def cancel_run(run_id: str) -> dict[str, Any]:
        return {"ok": engine.cancel_run(run_id)}

    @app.get("/api/chaos/rules")
    async def get_chaos() -> dict[str, Any]:
        return {"rules": [r.model_dump() for r in engine.chaos.get_rules()]}

    @app.post("/api/chaos/rules")
    async def set_chaos(req: ChaosRulesReq) -> dict[str, Any]:
        engine.chaos.set_rules(req.rules)
        return {"ok": True, "rules": [r.model_dump() for r in engine.chaos.get_rules()]}

    @app.post("/api/breakers/reset")
    async def reset_breakers() -> dict[str, Any]:
        engine.breakers.reset_all()
        return {"ok": True, "breakers": engine.breakers.list_snapshots()}

    @app.get("/api/dlq")
    async def list_dlq(unresolved_only: bool = Query(default=False)) -> dict[str, Any]:
        return {"items": store.list_dlq(unresolved_only=unresolved_only)}

    @app.post("/api/dlq/{dlq_id}/replay")
    async def replay_dlq(dlq_id: int) -> dict[str, Any]:
        try:
            return await engine.replay_dlq_item(dlq_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/workers")
    async def get_workers() -> dict[str, Any]:
        return {
            "workers": [w.to_dict() for w in engine.workers.values()],
            "breakers": engine.breakers.list_snapshots(),
            "completed_tasks_total": engine.completed_tasks_total,
            "crashes_recovered_total": engine.crashes_recovered_total,
        }

    @app.post("/api/benchmark/burst")
    async def benchmark_burst(req: BurstReq) -> dict[str, Any]:
        try:
            return await engine.run_burst_benchmark(req.workflow_id, count=req.count)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/events")
    async def list_events(run_id: str | None = None, limit: int = Query(default=200, ge=1, le=500)) -> dict[str, Any]:
        return {"events": store.list_events(run_id=run_id, limit=limit)}

    @app.get("/api/schedules")
    async def list_schedules() -> dict[str, Any]:
        return {"schedules": store.list_schedules()}

    @app.post("/api/schedules")
    async def create_schedule(req: ScheduleReq) -> dict[str, Any]:
        if store.get_workflow(req.workflow_id) is None:
            raise HTTPException(status_code=404, detail="Workflow not found")
        try:
            spec = parse_cron(req.cron_expr)
            nxt = next_cron_run(spec).timestamp()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.upsert_schedule(
            req.id, req.workflow_id, spec.expr, nxt, enabled=req.enabled, params=req.params
        )
        return {"ok": True, "id": req.id, "next_run_at": nxt}

    @app.delete("/api/schedules/{schedule_id}")
    async def remove_schedule(schedule_id: str) -> dict[str, Any]:
        if not store.delete_schedule(schedule_id):
            raise HTTPException(status_code=404, detail="Schedule not found")
        return {"ok": True}

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket) -> None:
        await ws.accept()
        q = engine.hub.subscribe()
        try:
            await ws.send_json(
                {
                    "type": "hello",
                    "workers": [w.to_dict() for w in engine.workers.values()],
                    "breakers": engine.breakers.list_snapshots(),
                    "runs": store.list_runs(limit=20),
                }
            )
            recv_task = asyncio.create_task(ws.receive())
            get_task = asyncio.create_task(q.get())
            try:
                while True:
                    done, _ = await asyncio.wait(
                        {recv_task, get_task}, return_when=asyncio.FIRST_COMPLETED
                    )
                    if recv_task in done:
                        frame = recv_task.result()
                        if frame.get("type") == "websocket.disconnect":
                            break
                        recv_task = asyncio.create_task(ws.receive())
                    if get_task in done:
                        msg = get_task.result()
                        await ws.send_json(msg)
                        get_task = asyncio.create_task(q.get())
            finally:
                for t in (recv_task, get_task):
                    if not t.done():
                        t.cancel()
                await asyncio.gather(recv_task, get_task, return_exceptions=True)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            engine.hub.unsubscribe(q)

    return app
