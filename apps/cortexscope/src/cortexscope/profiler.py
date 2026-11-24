"""Per-token latency, phase-splitting, quantile math, and energy attribution."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any

from .sensors import rapl_delta_uj


def compute_quantiles(values: list[float]) -> dict[str, float]:
    """Return min/p50/p95/p99/max/mean/stddev for non-empty latencies."""
    if not values:
        return {
            "min": 0.0,
            "p50": 0.0,
            "p95": 0.0,
            "p99": 0.0,
            "max": 0.0,
            "mean": 0.0,
            "stddev": 0.0,
        }
    ordered = sorted(float(v) for v in values)
    n = len(ordered)

    def pct(p: float) -> float:
        if n == 1:
            return ordered[0]
        idx = (p / 100.0) * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return ordered[lo]
        frac = idx - lo
        return ordered[lo] + (ordered[hi] - ordered[lo]) * frac

    mean = sum(ordered) / n
    var = sum((x - mean) ** 2 for x in ordered) / n
    return {
        "min": round(ordered[0], 3),
        "p50": round(pct(50.0), 3),
        "p95": round(pct(95.0), 3),
        "p99": round(pct(99.0), 3),
        "max": round(ordered[-1], 3),
        "mean": round(mean, 3),
        "stddev": round(math.sqrt(var), 3),
    }


def estimate_tokens(text: str) -> int:
    """Fast deterministic BPE-like token estimate (~3.6 chars or word+punct pieces)."""
    if not text:
        return 0
    chars_est = max(1, math.ceil(len(text) / 3.6))
    words_est = max(1, math.ceil(len(text.split()) * 1.32))
    return max(chars_est, words_est)


def estimate_context_budget(
    *,
    prompt: str = "",
    system_prompt: str = "",
    messages: list[dict[str, Any]] | None = None,
    context_length: int = 4096,
) -> dict[str, Any]:
    """Estimate context utilization and KV-cache pressure for a prompt payload."""
    ctx = max(1, int(context_length or 4096))
    parts: list[str] = []
    if system_prompt:
        parts.append(system_prompt)
    if prompt:
        parts.append(prompt)
    for msg in messages or []:
        content = msg.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
    est = sum(estimate_tokens(p) for p in parts) + (len(messages or []) * 4)
    util = round(min(999.9, (est / ctx) * 100.0), 2)
    if util < 50.0:
        pressure = "low"
    elif util < 80.0:
        pressure = "moderate"
    elif util < 95.0:
        pressure = "high"
    else:
        pressure = "critical"
    return {
        "estimated_tokens": est,
        "context_length": ctx,
        "remaining_tokens": max(0, ctx - est),
        "utilization_pct": util,
        "pressure_level": pressure,
    }


@dataclass(slots=True)
class TokenEvent:
    run_id: str
    seq: int
    phase: str
    token_text: str
    delta_ms: float
    elapsed_ms: float
    pkg_watts: float | None = None
    gt_mhz: int | None = None
    temp_c: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class StreamRunTracker:
    """Tracks per-chunk timing, think/answer splitting, and RAPL energy for one inference."""

    def __init__(
        self,
        *,
        run_id: str,
        model: str,
        source: str,
        endpoint: str,
        prompt_preview: str,
        start_ns: int,
        start_energy: dict[str, int] | None = None,
        max_energy: dict[str, int] | None = None,
        prompt_text: str = "",
        options_json: str = "{}",
    ) -> None:
        self.run_id = run_id
        self.model = model
        self.source = source
        self.endpoint = endpoint
        self.prompt_preview = prompt_preview[:240]
        self.prompt_text = prompt_text
        self.options_json = options_json
        self.start_ns = start_ns
        self.first_token_ns: int | None = None
        self.last_token_ns: int | None = None
        self.start_energy = dict(start_energy or {})
        self.max_energy = dict(max_energy or {})

        self.events: list[TokenEvent] = []
        self.itl_values: list[float] = []
        self.think_parts: list[str] = []
        self.answer_parts: list[str] = []
        self.think_tokens = 0
        self.answer_tokens = 0
        self._in_xml_think = False
        self.peak_temp_c: float | None = None
        self.peak_gt_mhz: int | None = None

    def observe_hw(self, *, pkg_watts: float | None = None, gt_mhz: int | None = None, temp_c: float | None = None) -> None:
        if temp_c is not None and (self.peak_temp_c is None or temp_c > self.peak_temp_c):
            self.peak_temp_c = temp_c
        if gt_mhz is not None and (self.peak_gt_mhz is None or gt_mhz > self.peak_gt_mhz):
            self.peak_gt_mhz = gt_mhz

    def _extract_phase_and_text(self, chunk: dict[str, Any]) -> tuple[str, str]:
        # Ollama /api/chat shape
        msg = chunk.get("message")
        if isinstance(msg, dict):
            think = msg.get("thinking")
            if isinstance(think, str) and think:
                return "think", think
            content = msg.get("content")
            if isinstance(content, str) and content:
                return self._classify_xml_think(content)

        # Ollama /api/generate shape
        think = chunk.get("thinking")
        if isinstance(think, str) and think:
            return "think", think
        resp = chunk.get("response")
        if isinstance(resp, str) and resp:
            return self._classify_xml_think(resp)

        # OpenAI /v1/chat/completions stream chunk shape
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            delta = choices[0].get("delta") or {}
            rc = delta.get("reasoning_content")
            if isinstance(rc, str) and rc:
                return "think", rc
            content = delta.get("content")
            if isinstance(content, str) and content:
                return self._classify_xml_think(content)

        return "answer", ""

    def _classify_xml_think(self, text: str) -> tuple[str, str]:
        if "<think>" in text:
            self._in_xml_think = True
        phase = "think" if self._in_xml_think else "answer"
        if "</think>" in text:
            self._in_xml_think = False
            if not text.strip().startswith("</think>") and text.rstrip().endswith("</think>"):
                phase = "think"
        return phase, text

    def on_chunk(
        self,
        chunk: dict[str, Any],
        *,
        now_ns: int,
        pkg_watts: float | None = None,
        gt_mhz: int | None = None,
        temp_c: float | None = None,
    ) -> TokenEvent | None:
        self.observe_hw(pkg_watts=pkg_watts, gt_mhz=gt_mhz, temp_c=temp_c)
        phase, text = self._extract_phase_and_text(chunk)
        if not text:
            return None

        if self.first_token_ns is None:
            self.first_token_ns = now_ns
            delta_ms = (now_ns - self.start_ns) / 1_000_000.0
        else:
            prev = self.last_token_ns or self.start_ns
            delta_ms = (now_ns - prev) / 1_000_000.0
            self.itl_values.append(delta_ms)

        self.last_token_ns = now_ns
        elapsed_ms = (now_ns - self.start_ns) / 1_000_000.0

        if phase == "think":
            self.think_tokens += 1
            self.think_parts.append(text)
        else:
            self.answer_tokens += 1
            self.answer_parts.append(text)

        event = TokenEvent(
            run_id=self.run_id,
            seq=len(self.events) + 1,
            phase=phase,
            token_text=text,
            delta_ms=round(max(0.0, delta_ms), 3),
            elapsed_ms=round(max(0.0, elapsed_ms), 3),
            pkg_watts=pkg_watts,
            gt_mhz=gt_mhz,
            temp_c=temp_c,
        )
        self.events.append(event)
        return event

    def _energy_summary(self, end_energy: dict[str, int] | None, total_ms: float) -> tuple[float | None, float | None, float | None]:
        if not end_energy or not self.start_energy:
            return None, None, None

        def joules_for(domain: str) -> float | None:
            if domain not in self.start_energy or domain not in end_energy:
                return None
            max_r = self.max_energy.get(domain, 0)
            duj = rapl_delta_uj(self.start_energy[domain], end_energy[domain], max_r)
            return round(duj / 1_000_000.0, 4)

        pkg_j = joules_for("package-0")
        psys_j = joules_for("psys")
        avg_w = round(pkg_j / (total_ms / 1000.0), 2) if (pkg_j is not None and total_ms > 0) else None
        return pkg_j, psys_j, avg_w

    def finalize(
        self,
        *,
        final_chunk: dict[str, Any] | None,
        now_ns: int,
        end_energy: dict[str, int] | None = None,
        peak_temp_c: float | None = None,
        peak_gt_mhz: int | None = None,
        status: str = "completed",
        error_msg: str | None = None,
    ) -> dict[str, Any]:
        self.observe_hw(gt_mhz=peak_gt_mhz, temp_c=peak_temp_c)
        fc = final_chunk or {}
        wall_total_ms = max(0.001, (now_ns - self.start_ns) / 1_000_000.0)

        load_ms = round(fc.get("load_duration", 0) / 1_000_000.0, 3) if fc.get("load_duration") else 0.0
        prompt_eval_ms = round(fc.get("prompt_eval_duration", 0) / 1_000_000.0, 3) if fc.get("prompt_eval_duration") else 0.0
        eval_ms = round(fc.get("eval_duration", 0) / 1_000_000.0, 3) if fc.get("eval_duration") else round(
            max(0.0, (now_ns - (self.first_token_ns or now_ns)) / 1_000_000.0), 3
        )
        total_ms = round(fc.get("total_duration", 0) / 1_000_000.0, 3) if fc.get("total_duration") else round(wall_total_ms, 3)

        ttft_ms = round(
            ((self.first_token_ns - self.start_ns) / 1_000_000.0) if self.first_token_ns else wall_total_ms,
            3,
        )
        prompt_tokens = int(fc.get("prompt_eval_count") or estimate_tokens(self.prompt_text))
        eval_tokens = int(fc.get("eval_count") or (self.think_tokens + self.answer_tokens))
        tps = round((eval_tokens / (eval_ms / 1000.0)), 2) if eval_ms > 0 and eval_tokens > 0 else 0.0

        q = compute_quantiles(self.itl_values)
        pkg_j, psys_j, avg_w = self._energy_summary(end_energy, wall_total_ms)
        j_per_100 = round((pkg_j * 100.0) / eval_tokens, 3) if (pkg_j is not None and eval_tokens > 0) else None

        return {
            "id": self.run_id,
            "source": self.source,
            "endpoint": self.endpoint,
            "model": self.model,
            "prompt_preview": self.prompt_preview,
            "prompt_text": self.prompt_text,
            "response_text": "".join(self.answer_parts),
            "thinking_text": "".join(self.think_parts),
            "options_json": self.options_json,
            "prompt_tokens": prompt_tokens,
            "eval_tokens": eval_tokens,
            "think_tokens": self.think_tokens,
            "answer_tokens": self.answer_tokens,
            "load_ms": load_ms,
            "prompt_eval_ms": prompt_eval_ms,
            "ttft_ms": ttft_ms,
            "eval_ms": eval_ms,
            "total_ms": total_ms,
            "tps": tps,
            "itl_p50_ms": q["p50"],
            "itl_p95_ms": q["p95"],
            "itl_p99_ms": q["p99"],
            "itl_jitter_ms": q["stddev"],
            "pkg_joules": pkg_j,
            "psys_joules": psys_j,
            "avg_pkg_watts": avg_w,
            "joules_per_100tok": j_per_100,
            "peak_temp_c": self.peak_temp_c,
            "peak_gt_mhz": self.peak_gt_mhz,
            "status": status,
            "error_msg": error_msg,
        }
