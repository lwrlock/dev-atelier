"""FastAPI application, WebSocket telemetry endpoint, Workbench, and Arena routes."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
import uuid

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .profiler import estimate_context_budget
from .proxy import RuntimeCoordinator, TelemetryHub
from .sensors import HardwareSampler
from .storage import TelemetryStore


class ContextEstimateReq(BaseModel):
    prompt: str = ""
    system_prompt: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)
    context_length: int = 4096


class WorkbenchRunReq(BaseModel):
    model: str
    prompt: str = Field(min_length=1)
    system_prompt: str = ""
    think: bool = False
    temperature: float = 0.2
    top_p: float = 0.9
    num_ctx: int = 4096
    num_predict: int = 512


class ArenaRunReq(BaseModel):
    prompt: str = Field(min_length=1)
    models: list[str] = Field(min_length=2, max_length=4)
    system_prompt: str = ""
    think: bool = False
    temperature: float = 0.2
    num_predict: int = 384


class ArenaVoteReq(BaseModel):
    winner_model: str
    notes: str = ""


class UnloadReq(BaseModel):
    model: str


def _project_static_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "static"


def create_app(settings: Settings | None = None, *, sysfs_root: Path | str = "/sys/class") -> FastAPI:
    cfg = settings or Settings.from_env()
    sampler = HardwareSampler(sysfs_root=sysfs_root)
    store = TelemetryStore(cfg.db_path)
    hub = TelemetryHub()
    coordinator = RuntimeCoordinator(
        upstream_url=cfg.upstream_url,
        sampler=sampler,
        store=store,
        hub=hub,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await coordinator.start()
        try:
            await coordinator.poll_once()
        except Exception:
            pass

        stop_event = asyncio.Event()

        async def poll_loop() -> None:
            while not stop_event.is_set():
                try:
                    await coordinator.poll_once()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=cfg.sample_interval_sec)
                except asyncio.TimeoutError:
                    continue

        task = asyncio.create_task(poll_loop())
        try:
            yield
        finally:
            stop_event.set()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            await coordinator.close()

    app = FastAPI(title="CortexScope", version="0.1.0", lifespan=lifespan)
    app.state.coordinator = coordinator
    app.state.settings = cfg

    static_dir = _project_static_dir()
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    async def index() -> Response:
        idx = static_dir / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"service": "CortexScope", "status": "ok"})

    @app.get("/cx/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "proxy_port": cfg.port,
            "upstream_url": cfg.upstream_url,
            "upstream_ok": coordinator.upstream_ok,
            "capabilities": coordinator.latest_hw.get("capabilities", {}),
        }

    @app.get("/cx/telemetry/now")
    async def telemetry_now(limit: int = Query(default=120, ge=1, le=1000)) -> dict[str, Any]:
        return {
            "hw": coordinator.latest_hw,
            "loaded_models": coordinator.latest_ps,
            "history": store.list_hw_samples(limit=limit),
        }

    @app.get("/cx/models")
    async def list_models() -> dict[str, Any]:
        try:
            tags = await coordinator.fetch_upstream_json("GET", "/api/tags")
            ps = await coordinator.fetch_upstream_json("GET", "/api/ps")
            coordinator.upstream_ok = True
        except Exception as exc:
            coordinator.upstream_ok = False
            raise HTTPException(status_code=502, detail=f"Ollama upstream unreachable: {exc}") from exc

        loaded_map = {m.get("name"): m for m in (ps.get("models") or [])}
        models = []
        for m in tags.get("models") or []:
            name = m.get("name")
            loaded = loaded_map.get(name)
            models.append(
                {
                    **m,
                    "loaded": loaded is not None,
                    "vram_bytes": loaded.get("size_vram", 0) if loaded else 0,
                    "expires_at": loaded.get("expires_at") if loaded else None,
                    "active_context": loaded.get("context_length") if loaded else None,
                }
            )
        return {"models": models, "loaded_models": ps.get("models") or []}

    @app.get("/cx/models/show")
    async def show_model(name: str = Query(min_length=1)) -> dict[str, Any]:
        try:
            return await coordinator.fetch_upstream_json("POST", "/api/show", {"model": name})
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Failed to inspect model {name}: {exc}") from exc

    @app.post("/cx/models/unload")
    async def unload_model(req: UnloadReq) -> dict[str, Any]:
        try:
            res = await coordinator.fetch_upstream_json(
                "POST", "/api/generate", {"model": req.model, "keep_alive": 0}
            )
            await coordinator.poll_once()
            return {"ok": True, "upstream": res}
        except Exception as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/cx/context/estimate")
    async def context_estimate(req: ContextEstimateReq) -> dict[str, Any]:
        return estimate_context_budget(
            prompt=req.prompt,
            system_prompt=req.system_prompt,
            messages=req.messages,
            context_length=req.context_length,
        )

    @app.get("/cx/runs")
    async def get_runs(
        limit: int = Query(default=100, ge=1, le=500),
        model: str | None = None,
        source: str | None = None,
        status: str | None = None,
        q: str | None = None,
    ) -> dict[str, Any]:
        return {"runs": store.list_runs(limit=limit, model=model, source=source, status=status, q=q)}

    @app.get("/cx/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        detail = store.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return detail

    @app.delete("/cx/runs/{run_id}")
    async def remove_run(run_id: str) -> dict[str, Any]:
        if not store.delete_run(run_id):
            raise HTTPException(status_code=404, detail="Run not found")
        return {"ok": True}

    @app.get("/cx/runs/{run_id}/export")
    async def export_run(run_id: str, format: str = Query(default="json")) -> Response:
        if format == "csv":
            csv_data = store.export_run_csv(run_id)
            if csv_data is None:
                raise HTTPException(status_code=404, detail="Run not found")
            return PlainTextResponse(
                csv_data,
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="cortexscope-{run_id}.csv"'},
            )
        detail = store.get_run(run_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return JSONResponse(
            detail,
            headers={"Content-Disposition": f'attachment; filename="cortexscope-{run_id}.json"'},
        )

    @app.get("/cx/stats")
    async def stats() -> dict[str, Any]:
        return store.get_stats()

    @app.post("/cx/workbench/run")
    async def workbench_run(req: WorkbenchRunReq) -> dict[str, Any]:
        return await coordinator.execute_internal_chat(
            source="workbench",
            model=req.model,
            prompt=req.prompt,
            system_prompt=req.system_prompt,
            think=req.think,
            options={
                "temperature": req.temperature,
                "top_p": req.top_p,
                "num_ctx": req.num_ctx,
                "num_predict": req.num_predict,
            },
        )

    @app.post("/cx/arena/run")
    async def arena_run(req: ArenaRunReq) -> dict[str, Any]:
        match_id = uuid.uuid4().hex[:12]
        results: list[dict[str, Any]] = []
        total = len(req.models)
        for idx, model in enumerate(req.models, start=1):
            hub.publish(
                {
                    "type": "arena_progress",
                    "match_id": match_id,
                    "step": idx,
                    "total": total,
                    "model": model,
                }
            )
            res = await coordinator.execute_internal_chat(
                source="arena",
                model=model,
                prompt=req.prompt,
                system_prompt=req.system_prompt,
                think=req.think,
                options={"temperature": req.temperature, "num_predict": req.num_predict},
            )
            results.append(
                {
                    "model": model,
                    "run_id": res["id"],
                    "status": res["status"],
                    "tps": res["tps"],
                    "ttft_ms": res["ttft_ms"],
                    "itl_p95_ms": res["itl_p95_ms"],
                    "eval_tokens": res["eval_tokens"],
                    "pkg_joules": res["pkg_joules"],
                    "joules_per_100tok": res["joules_per_100tok"],
                    "peak_temp_c": res["peak_temp_c"],
                    "response_text": res["response_text"],
                    "thinking_text": res["thinking_text"],
                    "error_msg": res["error_msg"],
                }
            )
        store.save_arena_match(
            match_id=match_id,
            prompt_text=req.prompt,
            models=req.models,
            results=results,
        )
        return {"id": match_id, "prompt_text": req.prompt, "models": req.models, "results": results}

    @app.get("/cx/arena/history")
    async def arena_history(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
        return {"matches": store.list_arena_matches(limit=limit)}

    @app.post("/cx/arena/{match_id}/vote")
    async def arena_vote(match_id: str, req: ArenaVoteReq) -> dict[str, Any]:
        if not store.vote_arena_match(match_id, req.winner_model, req.notes):
            raise HTTPException(status_code=404, detail="Arena match not found")
        return {"ok": True}

    @app.websocket("/ws/telemetry")
    async def ws_telemetry(ws: WebSocket) -> None:
        await ws.accept()
        q = hub.subscribe()
        try:
            await ws.send_json(
                {
                    "type": "hello",
                    "hw": coordinator.latest_hw,
                    "loaded_models": coordinator.latest_ps,
                    "runs": store.list_runs(limit=25),
                }
            )
            while True:
                msg = await q.get()
                await ws.send_json(msg)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            hub.unsubscribe(q)

    # Transparent Ollama & OpenAI-compatible inference proxy routes
    @app.post("/api/chat")
    async def proxy_chat(request: Request) -> Response:
        return await coordinator.proxy_inference(request, "/api/chat")

    @app.post("/api/generate")
    async def proxy_generate(request: Request) -> Response:
        return await coordinator.proxy_inference(request, "/api/generate")

    @app.post("/v1/chat/completions")
    async def proxy_v1_chat(request: Request) -> Response:
        return await coordinator.proxy_inference(request, "/v1/chat/completions")

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "DELETE"])
    async def proxy_passthrough(request: Request, rest: str) -> Response:
        if coordinator.session is None:
            return JSONResponse({"error": "upstream unavailable"}, status_code=503)
        body = await request.body()
        url = f"{cfg.upstream_url}/api/{rest}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        try:
            async with coordinator.session.request(
                request.method,
                url,
                data=body if body else None,
                headers={"Content-Type": request.headers.get("Content-Type", "application/json")},
                timeout=aiohttp.ClientTimeout(total=15.0),
            ) as resp:
                raw = await resp.read()
                return Response(
                    content=raw,
                    status_code=resp.status,
                    media_type=resp.headers.get("Content-Type", "application/json"),
                )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    return app
