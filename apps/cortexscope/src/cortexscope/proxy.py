"""Transparent streaming Ollama proxy, live WebSocket hub, and tracked inference runner."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, AsyncIterator

import aiohttp
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .profiler import StreamRunTracker
from .sensors import HardwareSampler
from .storage import TelemetryStore


class TelemetryHub:
    """Fan-out pub/sub hub for WebSocket telemetry with non-blocking backpressure."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=160)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subscribers):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return " ".join(parts)
    return ""


def extract_prompt_info(endpoint: str, payload: dict[str, Any]) -> tuple[str, str]:
    """Extract (prompt_preview, full_prompt_text) cleanly from Ollama/OpenAI payloads."""
    if endpoint.endswith("/generate"):
        text = str(payload.get("prompt") or "")
        return text.replace("\n", " ")[:240], text
    msgs = payload.get("messages")
    if isinstance(msgs, list) and msgs:
        last_user = ""
        all_lines = []
        for m in msgs:
            role = m.get("role", "user")
            text = _message_text(m.get("content", ""))
            if text:
                all_lines.append(f"[{role}] {text}")
                if role == "user":
                    last_user = text
        fallback = all_lines[-1] if all_lines else ""
        preview = (last_user or fallback).replace("\n", " ")[:240]
        return preview, "\n".join(all_lines)
    return "", ""


class RuntimeCoordinator:
    """Coordinates upstream HTTP session, hardware state, SQLite persistence, and proxy streams."""

    def __init__(
        self,
        *,
        upstream_url: str,
        sampler: HardwareSampler,
        store: TelemetryStore,
        hub: TelemetryHub,
    ) -> None:
        self.upstream_url = upstream_url
        self.sampler = sampler
        self.store = store
        self.hub = hub
        self.session: aiohttp.ClientSession | None = None
        self.latest_hw: dict[str, Any] = self.sampler.sample().to_dict()
        self.latest_ps: list[dict[str, Any]] = []
        self.upstream_ok: bool = False

    async def start(self) -> None:
        timeout = aiohttp.ClientTimeout(total=None, connect=10.0, sock_connect=10.0)
        self.session = aiohttp.ClientSession(timeout=timeout)

    async def close(self) -> None:
        if self.session is not None and not self.session.closed:
            await self.session.close()
        self.session = None

    def _hw_scalars(self) -> tuple[float | None, int | None, float | None]:
        return (
            self.latest_hw.get("pkg_watts"),
            self.latest_hw.get("gt0_act_mhz"),
            self.latest_hw.get("temp_c"),
        )

    async def fetch_upstream_json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.session is None:
            raise RuntimeError("HTTP client session not started")
        url = f"{self.upstream_url}{path}"
        async with self.session.request(method, url, json=payload, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
            data = await resp.json(content_type=None)
            if resp.status >= 400:
                raise aiohttp.ClientResponseError(
                    resp.request_info, resp.history, status=resp.status, message=str(data)
                )
            return data

    async def poll_once(self) -> dict[str, Any]:
        snap = self.sampler.sample().to_dict()
        loaded: list[dict[str, Any]] = []
        if self.session is not None:
            try:
                ps = await self.fetch_upstream_json("GET", "/api/ps")
                loaded = ps.get("models") or []
                self.upstream_ok = True
            except Exception:
                self.upstream_ok = False
        self.latest_hw = snap
        self.latest_ps = loaded
        sample_row = {**snap, "loaded_models": loaded}
        self.store.save_hw_sample(sample_row)
        self.hub.publish({"type": "hw_tick", "hw": snap, "loaded_models": loaded})
        return sample_row

    def _new_tracker(self, *, source: str, endpoint: str, payload: dict[str, Any]) -> StreamRunTracker:
        start_energy, max_energy = self.sampler.read_energy_counters()
        preview, full_prompt = extract_prompt_info(endpoint, payload)
        model = str(payload.get("model") or "unknown")
        opts = {
            k: payload[k]
            for k in ("options", "think", "temperature", "num_ctx", "num_predict")
            if k in payload
        }
        return StreamRunTracker(
            run_id=uuid.uuid4().hex[:12],
            model=model,
            source=source,
            endpoint=endpoint,
            prompt_preview=preview,
            prompt_text=full_prompt,
            options_json=json.dumps(opts),
            start_ns=time.monotonic_ns(),
            start_energy=start_energy,
            max_energy=max_energy,
        )

    def _finalize_and_persist(
        self,
        tracker: StreamRunTracker,
        *,
        final_chunk: dict[str, Any] | None,
        status: str,
        error_msg: str | None = None,
    ) -> dict[str, Any]:
        end_energy, _ = self.sampler.read_energy_counters()
        hw_now = self.sampler.sample().to_dict()
        self.latest_hw = hw_now
        summary = tracker.finalize(
            final_chunk=final_chunk,
            now_ns=time.monotonic_ns(),
            end_energy=end_energy,
            peak_temp_c=hw_now.get("temp_c"),
            peak_gt_mhz=hw_now.get("gt0_act_mhz"),
            status=status,
            error_msg=error_msg,
        )
        events = [e.to_dict() for e in tracker.events]
        self.store.save_run(summary, events)
        self.hub.publish({"type": "run_complete", "run": summary})
        return {**summary, "events": events}

    async def proxy_inference(self, request: Request, endpoint: str) -> Response:
        if self.session is None:
            return JSONResponse({"error": "upstream session unavailable"}, status_code=503)
        raw_body = await request.body()
        try:
            payload = json.loads(raw_body) if raw_body else {}
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid JSON request body"}, status_code=400)

        stream_requested = bool(payload.get("stream", not endpoint.startswith("/v1/")))
        tracker = self._new_tracker(source="proxy", endpoint=endpoint, payload=payload)
        self.hub.publish(
            {
                "type": "run_start",
                "run": {
                    "id": tracker.run_id,
                    "source": "proxy",
                    "endpoint": endpoint,
                    "model": tracker.model,
                    "prompt_preview": tracker.prompt_preview,
                },
            }
        )

        upstream_url = f"{self.upstream_url}{endpoint}"
        headers = {"Content-Type": "application/json"}

        if not stream_requested:
            try:
                async with self.session.post(upstream_url, data=raw_body, headers=headers) as resp:
                    body = await resp.read()
                    try:
                        data = json.loads(body)
                        if resp.status < 400:
                            tracker.on_chunk(data, now_ns=time.monotonic_ns())
                            self._finalize_and_persist(tracker, final_chunk=data, status="completed")
                        else:
                            self._finalize_and_persist(
                                tracker, final_chunk=None, status="error", error_msg=f"HTTP {resp.status}"
                            )
                    except json.JSONDecodeError:
                        self._finalize_and_persist(
                            tracker, final_chunk=None, status="error", error_msg="Non-JSON response"
                        )
                    return Response(
                        content=body,
                        status_code=resp.status,
                        media_type=resp.headers.get("Content-Type", "application/json"),
                    )
            except Exception as exc:
                self._finalize_and_persist(tracker, final_chunk=None, status="error", error_msg=str(exc))
                return JSONResponse({"error": str(exc)}, status_code=502)

        resp_cm = self.session.post(upstream_url, data=raw_body, headers=headers)
        try:
            upstream_resp = await resp_cm.__aenter__()
        except Exception as exc:
            self._finalize_and_persist(tracker, final_chunk=None, status="error", error_msg=str(exc))
            return JSONResponse({"error": str(exc)}, status_code=502)

        media_type = upstream_resp.headers.get(
            "Content-Type",
            "text/event-stream" if endpoint.startswith("/v1/") else "application/x-ndjson",
        )
        status_code = upstream_resp.status

        async def stream_gen() -> AsyncIterator[bytes]:
            final_chunk: dict[str, Any] | None = None
            completed = False
            error_msg: str | None = None
            buf = b""
            try:
                async for chunk_bytes in upstream_resp.content.iter_any():
                    if not chunk_bytes:
                        continue
                    yield chunk_bytes
                    buf += chunk_bytes
                    while b"\n" in buf:
                        line_b, buf = buf.split(b"\n", 1)
                        parsed = self._consume_line(tracker, line_b, is_sse=endpoint.startswith("/v1/"))
                        if parsed is not None and parsed.get("done") is True:
                            final_chunk = parsed
                if buf.strip():
                    parsed = self._consume_line(tracker, buf, is_sse=endpoint.startswith("/v1/"))
                    if parsed is not None and parsed.get("done") is True:
                        final_chunk = parsed
                completed = status_code < 400
                if not completed:
                    error_msg = f"HTTP {status_code}"
            except asyncio.CancelledError:
                error_msg = "Client disconnected"
                raise
            except Exception as exc:
                error_msg = str(exc)
            finally:
                try:
                    await resp_cm.__aexit__(None, None, None)
                except Exception:
                    pass
                final_status = "completed" if completed else ("error" if error_msg and error_msg != "Client disconnected" else "aborted")
                self._finalize_and_persist(
                    tracker, final_chunk=final_chunk, status=final_status, error_msg=error_msg
                )

        return StreamingResponse(stream_gen(), status_code=status_code, media_type=media_type)

    def _consume_line(
        self, tracker: StreamRunTracker, line_b: bytes, *, is_sse: bool
    ) -> dict[str, Any] | None:
        parsed = self._parse_stream_line(line_b, is_sse=is_sse)
        if parsed is None:
            return None
        w, gt, temp = self._hw_scalars()
        ev = tracker.on_chunk(parsed, now_ns=time.monotonic_ns(), pkg_watts=w, gt_mhz=gt, temp_c=temp)
        if ev is not None:
            self.hub.publish({"type": "token_chunk", "event": ev.to_dict()})
        return parsed

    @staticmethod
    def _parse_stream_line(line_b: bytes, *, is_sse: bool) -> dict[str, Any] | None:
        text = line_b.decode("utf-8", errors="replace").strip()
        if not text:
            return None
        if is_sse:
            if not text.startswith("data:"):
                return None
            data_str = text[5:].strip()
            if data_str == "[DONE]":
                return {"done": True}
            text = data_str
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None

    async def execute_internal_chat(
        self,
        *,
        source: str,
        model: str,
        prompt: str,
        system_prompt: str = "",
        think: bool = False,
        options: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run a controlled prompt against upstream /api/chat with full per-token profiling."""
        if self.session is None:
            raise RuntimeError("Upstream session not started")
        messages: list[dict[str, str]] = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt.strip()})
        messages.append({"role": "user", "content": prompt})

        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
        }
        if options:
            payload["options"] = options

        tracker = self._new_tracker(source=source, endpoint="/api/chat", payload=payload)
        self.hub.publish(
            {
                "type": "run_start",
                "run": {
                    "id": tracker.run_id,
                    "source": source,
                    "endpoint": "/api/chat",
                    "model": model,
                    "prompt_preview": tracker.prompt_preview,
                },
            }
        )

        final_chunk: dict[str, Any] | None = None
        buf = b""
        try:
            async with self.session.post(f"{self.upstream_url}/api/chat", json=payload) as resp:
                if resp.status >= 400:
                    err_text = await resp.text()
                    return self._finalize_and_persist(
                        tracker, final_chunk=None, status="error", error_msg=f"HTTP {resp.status}: {err_text[:200]}"
                    )
                async for chunk_bytes in resp.content.iter_any():
                    buf += chunk_bytes
                    while b"\n" in buf:
                        line_b, buf = buf.split(b"\n", 1)
                        parsed = self._consume_line(tracker, line_b, is_sse=False)
                        if parsed is not None and parsed.get("done") is True:
                            final_chunk = parsed
                if buf.strip():
                    parsed = self._consume_line(tracker, buf, is_sse=False)
                    if parsed is not None and parsed.get("done") is True:
                        final_chunk = parsed
            return self._finalize_and_persist(tracker, final_chunk=final_chunk, status="completed")
        except asyncio.CancelledError:
            self._finalize_and_persist(tracker, final_chunk=final_chunk, status="aborted", error_msg="Cancelled")
            raise
        except Exception as exc:
            return self._finalize_and_persist(tracker, final_chunk=final_chunk, status="error", error_msg=str(exc))
