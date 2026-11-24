"""Sandboxed built-in, webhook, shell-allowlisted, and AI task executors."""

from __future__ import annotations

import asyncio
import math
import os
import random
import time
from typing import Any

import aiohttp

from .models import ExecutorKind, TaskSpec, validate_webhook_url


class SecurityViolationError(PermissionError):
    """Raised when a task payload attempts a disallowed shell command or unsafe URL."""


class WorkerCrashSimulated(RuntimeError):
    """Internal exception raised when ChaosEngine simulates a mid-flight worker crash."""


SAFE_BINARIES = {"uname", "uptime", "date", "df", "free"}


async def run_shell_safe(payload: dict[str, Any], *, timeout_sec: float = 5.0) -> dict[str, Any]:
    """Execute only allowlisted diagnostic binaries with shell=False and hard timeout."""
    cmd = payload.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        raise SecurityViolationError("Payload 'cmd' must be a non-empty list of strings.")
    binary = os.path.basename(cmd[0])
    if cmd[0] != binary or binary not in SAFE_BINARIES:
        raise SecurityViolationError(f"Binary {cmd[0]!r} is not in SAFE_BINARIES allowlist.")

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise TimeoutError(f"Command {binary} timed out after {timeout_sec}s")

    stdout = out_b.decode("utf-8", errors="replace").strip()
    stderr = err_b.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"{binary} exited {proc.returncode}: {stderr[:200]}")
    return {"output": {"returncode": proc.returncode, "binary": binary}, "stdout": stdout}


async def run_http_webhook(payload: dict[str, Any], *, timeout_sec: float = 5.0) -> dict[str, Any]:
    url = validate_webhook_url(
        str(payload.get("url", "")), allow_loopback=bool(payload.get("allow_loopback", False))
    )
    method = str(payload.get("method", "POST")).upper()
    if method not in {"GET", "POST", "PUT"}:
        raise SecurityViolationError(f"Disallowed HTTP method: {method}")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as sess:
        async with sess.request(method, url, json=payload.get("body")) as resp:
            text = (await resp.text())[:500]
            if resp.status >= 400:
                raise RuntimeError(f"Webhook HTTP {resp.status}: {text}")
            return {"output": {"status": resp.status, "body": text}, "stdout": f"HTTP {resp.status} -> {url}"}


async def run_ollama_prompt(payload: dict[str, Any], *, timeout_sec: float = 15.0) -> dict[str, Any]:
    url = validate_webhook_url(
        str(payload.get("url", "http://127.0.0.1:11434/api/generate")), allow_loopback=True
    )
    req_body = {
        "model": str(payload.get("model", "qwen3.5-hizli:latest")),
        "prompt": str(payload.get("prompt", "Summarize status in 1 sentence.")),
        "stream": False,
        "think": False,
        "options": {"num_predict": int(payload.get("num_predict", 64))},
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_sec)) as sess:
        async with sess.post(url, json=req_body) as resp:
            data = await resp.json(content_type=None)
            text = str(data.get("response", "")).strip()
            return {"output": {"model": req_body["model"], "response": text}, "stdout": text}


async def _builtin_handler(
    handler: str,
    task: TaskSpec,
    upstream_outputs: dict[str, Any],
    run_params: dict[str, Any],
) -> dict[str, Any]:
    # Scaled deterministic async simulation wait (keeps burst tests fast while realistic)
    sleep_s = min(0.6, max(0.012, task.estimated_ms / 1000.0))
    await asyncio.sleep(sleep_s)

    merged_score = task.payload.get("score", run_params.get("risk_score"))
    if handler == "fraud_score":
        score = float(merged_score if merged_score is not None else round(random.uniform(0.05, 0.35), 3))
        risk = "high" if score >= 0.7 else "low"
        return {
            "output": {"score": score, "risk": risk, "model": "xgb-fraud-v4"},
            "stdout": f"[fraud_score] score={score:.3f} risk={risk}",
        }

    if handler == "inventory_reserve":
        sku = task.payload.get("sku", run_params.get("sku", "SKU-ARL-900"))
        qty = int(task.payload.get("qty", run_params.get("qty", 1)))
        res_id = f"res_{int(time.time() * 1000) % 100000}"
        return {
            "output": {"reservation_id": res_id, "sku": sku, "qty": qty, "reserved": True},
            "stdout": f"[inventory] reserved {qty}x {sku} (lock={res_id})",
        }

    if handler == "inventory_release":
        return {
            "output": {"released": True, "compensated_at": time.time()},
            "stdout": "[saga:inventory_release] lock released cleanly",
        }

    if handler == "payment_charge":
        amt = float(run_params.get("amount", task.payload.get("amount", 149.0)))
        tx = f"ch_{int(time.time() * 1000) % 1000000}"
        return {
            "output": {"tx_id": tx, "amount_usd": amt, "captured": True},
            "stdout": f"[payments] captured ${amt:.2f} tx={tx}",
        }

    if handler == "payment_refund":
        return {
            "output": {"refunded": True, "compensated_at": time.time()},
            "stdout": "[saga:payment_refund] full refund issued idempotently",
        }

    if handler == "ml_train_fold":
        lr = float(task.payload.get("lr", 0.001))
        val_loss = round(0.18 + abs(math.log10(max(1e-5, lr)) + 3.0) * 0.07 + random.uniform(0.0, 0.02), 4)
        return {
            "output": {"lr": lr, "val_loss": val_loss, "accuracy": round(1.0 - val_loss, 4)},
            "stdout": f"[train] lr={lr} val_loss={val_loss} acc={1.0 - val_loss:.4f}",
        }

    if handler == "ml_select_champion":
        losses = [
            (nid, out.get("val_loss", 1.0))
            for nid, out in upstream_outputs.items()
            if isinstance(out, dict) and "val_loss" in out
        ]
        best = min(losses, key=lambda x: x[1], default=("none", 0.0))
        return {
            "output": {"champion": best[0], "best_val_loss": best[1]},
            "stdout": f"[eval] champion={best[0]} val_loss={best[1]}",
        }

    if handler == "canary_deploy":
        pct = int(task.payload.get("weight_pct", 10))
        return {
            "output": {"canary_weight_pct": pct, "healthy": True},
            "stdout": f"[rollout] shifted {pct}% traffic -> canary healthy",
        }

    if handler == "canary_rollback":
        return {
            "output": {"canary_weight_pct": 0, "rolled_back": True},
            "stdout": "[saga:canary_rollback] drained canary back to 0%",
        }

    return {
        "output": {"handler": handler, "ok": True, "upstream_keys": sorted(upstream_outputs.keys())},
        "stdout": f"[{handler}] completed in {int(sleep_s * 1000)}ms",
    }


async def execute_task_step(
    task: TaskSpec,
    *,
    handler_name: str,
    upstream_outputs: dict[str, Any],
    run_params: dict[str, Any],
) -> dict[str, Any]:
    """Execute one forward or compensating task step with strict timeout enforcement."""
    if task.executor == ExecutorKind.SHELL_SAFE:
        return await run_shell_safe(task.payload, timeout_sec=task.timeout_sec)
    if task.executor == ExecutorKind.HTTP_WEBHOOK:
        return await asyncio.wait_for(
            run_http_webhook(task.payload, timeout_sec=task.timeout_sec), timeout=task.timeout_sec
        )
    if task.executor == ExecutorKind.OLLAMA_PROMPT:
        return await asyncio.wait_for(
            run_ollama_prompt(task.payload, timeout_sec=task.timeout_sec), timeout=task.timeout_sec
        )
    return await asyncio.wait_for(
        _builtin_handler(handler_name, task, upstream_outputs, run_params), timeout=task.timeout_sec
    )
