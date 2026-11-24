import asyncio
from contextlib import asynccontextmanager
import json
import socket
from pathlib import Path

import aiohttp
from aiohttp import web
import pytest
import uvicorn
import websockets


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def _start_fake_ollama():
    abort_seen = asyncio.Event()

    async def handle_tags(req):
        return web.json_response(
            {
                "query_seen": req.query.get("filter", ""),
                "models": [
                    {
                        "name": "model-a:latest",
                        "size": 2_000_000_000,
                        "details": {"parameter_size": "4.2B", "quantization_level": "Q4_K_M", "context_length": 4096},
                    },
                    {
                        "name": "model-b:latest",
                        "size": 4_000_000_000,
                        "details": {"parameter_size": "8.2B", "quantization_level": "Q4_K_M", "context_length": 8192},
                    },
                ]
            }
        )

    async def handle_ps(_req):
        return web.json_response(
            {
                "models": [
                    {
                        "name": "model-a:latest",
                        "size": 2_000_000_000,
                        "size_vram": 1_800_000_000,
                        "context_length": 4096,
                        "expires_at": "2026-11-23T05:00:00Z",
                    }
                ]
            }
        )

    async def handle_show(req):
        body = await req.json()
        return web.json_response(
            {
                "modelfile": f"FROM {body.get('model', 'model-a:latest')}\nPARAMETER temperature 0.2\n",
                "system": "Be brief.",
                "parameters": "temperature 0.2",
                "template": "{{ .Prompt }}",
                "capabilities": ["completion", "thinking"],
                "details": {"parameter_size": "4.2B", "quantization_level": "Q4_K_M"},
                "model_info": {"general.architecture": "qwen35", "qwen35.context_length": 4096},
            }
        )

    async def handle_chat(req):
        body = await req.json()
        model = body.get("model", "model-a:latest")
        slow = body.get("slow_abort_test", False)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(req)
        chunks = [
            {"model": model, "message": {"role": "assistant", "thinking": "hmm "}, "done": False},
            {"model": model, "message": {"role": "assistant", "content": "=HYPERLINK"}, "done": False},
            {"model": model, "message": {"role": "assistant", "content": " safe"}, "done": False},
        ]
        try:
            for idx, c in enumerate(chunks):
                await resp.write((json.dumps(c) + "\n").encode())
                if slow and idx == 1:
                    await asyncio.sleep(2.5)
                else:
                    await asyncio.sleep(0.015)
            final = {
                "model": model,
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "prompt_eval_count": 14,
                "eval_count": 3,
                "load_duration": 5_000_000,
                "prompt_eval_duration": 15_000_000,
                "eval_duration": 60_000_000,
                "total_duration": 90_000_000,
            }
            await resp.write((json.dumps(final) + "\n").encode())
            await resp.write_eof()
        except (ConnectionResetError, asyncio.CancelledError):
            abort_seen.set()
        return resp

    async def handle_v1_chat(req):
        body = await req.json()
        model = body.get("model", "model-a:latest")
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(req)
        sse_items = [
            {"choices": [{"delta": {"reasoning_content": "plan "}}]},
            {"choices": [{"delta": {"content": "ready"}}]},
        ]
        for item in sse_items:
            await resp.write(f"data: {json.dumps(item)}\n\n".encode())
            await asyncio.sleep(0.01)
        await resp.write(b"data: [DONE]\n\n")
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_get("/api/tags", handle_tags)
    app.router.add_get("/api/ps", handle_ps)
    app.router.add_post("/api/show", handle_show)
    app.router.add_post("/api/chat", handle_chat)
    app.router.add_post("/v1/chat/completions", handle_v1_chat)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, port, abort_seen


@asynccontextmanager
async def live_stack_ctx(tmp_path: Path):
    from cortexscope.app import create_app
    from cortexscope.config import Settings

    runner, upstream_port, abort_seen = await _start_fake_ollama()
    proxy_port = _free_port()
    settings = Settings(
        host="127.0.0.1",
        port=proxy_port,
        upstream_url=f"http://127.0.0.1:{upstream_port}",
        db_path=tmp_path / "live.db",
        sample_interval_sec=0.25,
    )
    fastapi_app = create_app(settings, sysfs_root=tmp_path)
    config = uvicorn.Config(fastapi_app, host="127.0.0.1", port=proxy_port, log_level="warning")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None
    serve_task = asyncio.create_task(server.serve())

    while not server.started:
        if serve_task.done():
            serve_task.result()
        await asyncio.sleep(0.02)

    try:
        yield f"http://127.0.0.1:{proxy_port}", abort_seen
    finally:
        server.should_exit = True
        await serve_task
        await runner.cleanup()


def test_proxy_chat_stream_ws_csv_workbench_and_arena(tmp_path: Path):
    async def scenario():
        async with live_stack_ctx(tmp_path) as (base_url, _):
            ws_url = base_url.replace("http://", "ws://") + "/ws/telemetry"
            ws_events = []
            async with websockets.connect(ws_url) as ws:
                hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                assert hello["type"] == "hello"

                async with aiohttp.ClientSession() as session:
                    # 1. Proxied Ollama /api/chat stream
                    async with session.post(
                        f"{base_url}/api/chat",
                        json={"model": "model-a:latest", "messages": [{"role": "user", "content": "hi"}]},
                    ) as resp:
                        assert resp.status == 200
                        raw = await resp.text()
                        lines = [json.loads(line) for line in raw.strip().splitlines()]
                        assert lines[-1]["done"] is True

                    # Drain ws events until run_complete
                    for _ in range(30):
                        msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=2.0))
                        ws_events.append(msg)
                        if msg.get("type") == "run_complete":
                            break
                    assert any(m.get("type") == "run_complete" for m in ws_events)

                    # 2. Verify runs list + CSV export formula escaping
                    async with session.get(f"{base_url}/cx/runs") as resp:
                        runs = (await resp.json())["runs"]
                        assert len(runs) == 1
                        run_id = runs[0]["id"]
                        assert runs[0]["think_tokens"] == 1
                        assert runs[0]["answer_tokens"] == 2
                        assert runs[0]["tps"] == pytest.approx(50.0, rel=0.1)

                    async with session.get(f"{base_url}/cx/runs/{run_id}/export?format=csv") as resp:
                        csv_text = await resp.text()
                        assert "'=HYPERLINK" in csv_text

                    # 3. OpenAI /v1/chat/completions SSE proxy
                    async with session.post(
                        f"{base_url}/v1/chat/completions",
                        json={"model": "model-a:latest", "messages": [{"role": "user", "content": "ping"}], "stream": True},
                    ) as resp:
                        assert resp.status == 200
                        sse_body = await resp.text()
                        assert "data: [DONE]" in sse_body

                    # 4. Workbench + Arena + Vote
                    async with session.post(
                        f"{base_url}/cx/workbench/run",
                        json={"model": "model-a:latest", "prompt": "Say hi", "think": False, "temperature": 0.1},
                    ) as resp:
                        wb = await resp.json()
                        assert wb["status"] == "completed"

                    async with session.post(
                        f"{base_url}/cx/arena/run",
                        json={"prompt": "Compare models", "models": ["model-a:latest", "model-b:latest"]},
                    ) as resp:
                        arena = await resp.json()
                        assert len(arena["results"]) == 2
                        match_id = arena["id"]

                    async with session.post(
                        f"{base_url}/cx/arena/{match_id}/vote",
                        json={"winner_model": "model-b:latest", "notes": "Faster TTFT"},
                    ) as resp:
                        assert (await resp.json())["ok"] is True

                    async with session.get(f"{base_url}/cx/models") as resp:
                        models = (await resp.json())["models"]
                        assert len(models) == 2

                    async with session.get(f"{base_url}/cx/stats") as resp:
                        stats = await resp.json()
                        assert len(stats["by_model"]) >= 2

    asyncio.run(scenario())


def test_proxy_client_abort_records_aborted_run(tmp_path: Path):
    async def scenario():
        async with live_stack_ctx(tmp_path) as (base_url, _):
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{base_url}/api/chat",
                    json={
                        "model": "model-a:latest",
                        "messages": [{"role": "user", "content": "abort me"}],
                        "slow_abort_test": True,
                    },
                ) as resp:
                    chunk = await resp.content.readline()
                    assert b"hmm" in chunk
                    resp.close()

            await asyncio.sleep(0.25)
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/cx/runs") as resp:
                    runs = (await resp.json())["runs"]
                    assert any(r["status"] == "aborted" for r in runs)

    asyncio.run(scenario())


def test_multipart_prompt_extraction_and_query_passthrough(tmp_path: Path):
    from cortexscope.proxy import extract_prompt_info

    preview, full = extract_prompt_info(
        "/v1/chat/completions",
        {"messages": [{"role": "user", "content": [{"type": "text", "text": "Inspect kernel"}, {"type": "image_url", "image_url": "..."}]}]},
    )
    assert "Inspect kernel" in preview
    assert "[user] Inspect kernel" in full

    async def scenario():
        async with live_stack_ctx(tmp_path) as (base_url, _):
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base_url}/api/tags?filter=qwen") as resp:
                    data = await resp.json()
                    assert data["query_seen"] == "qwen"

    asyncio.run(scenario())

