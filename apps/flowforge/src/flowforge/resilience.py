"""Circuit breakers, token-bucket rate limiters, backoff math, and Chaos Fault Engine."""

from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .models import BreakerState, ChaosRule


def compute_backoff_ms(
    *, attempt: int, base_ms: int = 120, max_ms: int = 5000, jitter_ratio: float = 0.2, seed: int | None = None
) -> float:
    """Exponential backoff with bounded jitter."""
    exp = min(max_ms, base_ms * (2 ** max(0, attempt - 1)))
    if jitter_ratio <= 0:
        return float(exp)
    rng = random.Random(seed) if seed is not None else random
    spread = exp * jitter_ratio
    return round(max(5.0, min(float(max_ms), exp + rng.uniform(-spread, spread))), 2)


class CircuitBreaker:
    """Three-state dependency circuit breaker (CLOSED -> OPEN -> HALF_OPEN)."""

    def __init__(
        self,
        name: str,
        *,
        failure_threshold: int = 3,
        recovery_sec: float = 12.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_sec = max(0.5, recovery_sec)
        self._clock = clock
        self._lock = threading.Lock()
        self.state = BreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.opened_at: float | None = None
        self.last_changed_wall: float = time.time()

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == BreakerState.CLOSED:
                return True
            if self.state == BreakerState.OPEN:
                if self.opened_at is not None and (self._clock() - self.opened_at) >= self.recovery_sec:
                    self.state = BreakerState.HALF_OPEN
                    self.last_changed_wall = time.time()
                    return True
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            self.success_count += 1
            self.failure_count = 0
            if self.state != BreakerState.CLOSED:
                self.state = BreakerState.CLOSED
                self.opened_at = None
                self.last_changed_wall = time.time()

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            if self.state == BreakerState.HALF_OPEN or self.failure_count >= self.failure_threshold:
                self.state = BreakerState.OPEN
                self.opened_at = self._clock()
                self.last_changed_wall = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            cooldown_rem = 0.0
            if self.state == BreakerState.OPEN and self.opened_at is not None:
                cooldown_rem = max(0.0, round(self.recovery_sec - (self._clock() - self.opened_at), 2))
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "failure_threshold": self.failure_threshold,
                "success_count": self.success_count,
                "cooldown_remaining_sec": cooldown_rem,
            }


class CircuitBreakerRegistry:
    def __init__(self, failure_threshold: int = 3, recovery_sec: float = 10.0) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_sec = recovery_sec
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        key = name or "default"
        with self._lock:
            if key not in self._breakers:
                self._breakers[key] = CircuitBreaker(
                    key, failure_threshold=self._failure_threshold, recovery_sec=self._recovery_sec
                )
            return self._breakers[key]

    def reset_all(self) -> None:
        with self._lock:
            for b in self._breakers.values():
                b.record_success()

    def list_snapshots(self) -> list[dict]:
        with self._lock:
            items = list(self._breakers.values())
        return [b.snapshot() for b in items]


class TokenBucketLimiter:
    """Async token-bucket rate limiter."""

    def __init__(self, rate_per_sec: float = 50.0, burst: float = 20.0) -> None:
        self.rate = max(0.5, float(rate_per_sec))
        self.capacity = max(1.0, float(burst))
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, count: float = 1.0) -> float:
        waited = 0.0
        while True:
            async with self._lock:
                now = time.monotonic()
                elapsed = max(0.0, now - self.updated_at)
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.updated_at = now
                if self.tokens >= count:
                    self.tokens -= count
                    return round(waited * 1000.0, 2)
                deficit = count - self.tokens
                sleep_for = deficit / self.rate
            waited += sleep_for
            await asyncio.sleep(min(0.25, sleep_for))


@dataclass(slots=True)
class ChaosDecision:
    latency_ms: int = 0
    force_fail: bool = False
    crash_worker: bool = False


class ChaosEngine:
    """Thread-safe active fault-injection rules evaluator."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: list[ChaosRule] = []
        self._consumed_crashes: set[tuple[str, str]] = set()

    def set_rules(self, rules: list[ChaosRule]) -> None:
        with self._lock:
            self._rules = [r.model_copy() for r in rules]
            self._consumed_crashes.clear()

    def get_rules(self) -> list[ChaosRule]:
        with self._lock:
            return [r.model_copy() for r in self._rules]

    def clear(self) -> None:
        self.set_rules([])

    def evaluate(self, run_id: str, node_id: str) -> ChaosDecision:
        with self._lock:
            active = [r for r in self._rules if r.enabled and (r.node_id == "*" or r.node_id == node_id)]
            if not active:
                return ChaosDecision()
            latency = max(r.latency_ms for r in active)
            fail_prob = max(r.fail_prob for r in active)
            crash = False
            for r in active:
                if r.crash_worker_once and (run_id, node_id) not in self._consumed_crashes:
                    self._consumed_crashes.add((run_id, node_id))
                    crash = True
                    break
        force_fail = fail_prob >= 1.0 or (fail_prob > 0.0 and random.random() < fail_prob)
        return ChaosDecision(latency_ms=latency, force_fail=force_fail, crash_worker=crash)
