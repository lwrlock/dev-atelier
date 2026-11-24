from datetime import datetime, timezone
import pytest


def test_dag_compiler_detects_cycles_and_computes_critical_path():
    from flowforge.models import EdgeSpec, TaskSpec, WorkflowSpec
    from flowforge.graph import CycleError, compile_dag

    # Diamond DAG: start(50) -> fast(40) -> merge(30), start(50) -> slow(150) -> merge(30)
    wf = WorkflowSpec(
        id="wf-diamond",
        name="Diamond",
        nodes=[
            TaskSpec(id="start", label="Start", handler="sim_compute", estimated_ms=50),
            TaskSpec(id="fast", label="Fast", handler="sim_compute", estimated_ms=40),
            TaskSpec(id="slow", label="Slow", handler="sim_compute", estimated_ms=150),
            TaskSpec(id="merge", label="Merge", handler="sim_compute", estimated_ms=30),
        ],
        edges=[
            EdgeSpec(source="start", target="fast"),
            EdgeSpec(source="start", target="slow"),
            EdgeSpec(source="fast", target="merge"),
            EdgeSpec(source="slow", target="merge"),
        ],
    )
    compiled = compile_dag(wf)
    assert compiled.makespan_ms == 230
    assert compiled.critical_path == ["start", "slow", "merge"]
    assert compiled.cpm["fast"].slack_ms == 110
    assert compiled.cpm["slow"].slack_ms == 0
    assert compiled.layers["start"] == 0
    assert compiled.layers["merge"] == 2

    # Multi-node cycle rejection
    cyclic = WorkflowSpec(
        id="wf-cyc",
        name="Cyclic",
        nodes=[
            TaskSpec(id="a", label="A"),
            TaskSpec(id="b", label="B"),
            TaskSpec(id="c", label="C"),
        ],
        edges=[
            EdgeSpec(source="a", target="b"),
            EdgeSpec(source="b", target="c"),
            EdgeSpec(source="c", target="a"),
        ],
    )
    with pytest.raises(CycleError) as exc_info:
        compile_dag(cyclic)
    assert set(exc_info.value.cycle) == {"a", "b", "c"}


def test_safe_eval_condition_blocks_arbitrary_code():
    from flowforge.graph import UnsafeExpressionError, safe_eval_condition

    ctx = {"upstream": {"score": 0.92, "risk": "low"}, "attempt": 1}
    assert safe_eval_condition("upstream.score >= 0.8 and upstream.risk == 'low'", ctx) is True
    assert safe_eval_condition("upstream.score < 0.5", ctx) is False

    with pytest.raises(UnsafeExpressionError):
        safe_eval_condition("__import__('os').system('echo pwned')", ctx)
    with pytest.raises(UnsafeExpressionError):
        safe_eval_condition("().__class__.__bases__[0]", ctx)


def test_webhook_ssrf_guard():
    from flowforge.models import validate_webhook_url

    assert validate_webhook_url("https://hooks.example.org/notify") == "https://hooks.example.org/notify"
    with pytest.raises(ValueError):
        validate_webhook_url("http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError):
        validate_webhook_url("file:///etc/shadow")
    with pytest.raises(ValueError):
        validate_webhook_url("http://127.0.0.1:11434/api/tags", allow_loopback=False)
    assert validate_webhook_url("http://127.0.0.1:11434/api/tags", allow_loopback=True).startswith("http://127.0.0.1")


def test_circuit_breaker_transitions_and_chaos_crash_once():
    from flowforge.models import BreakerState, ChaosRule
    from flowforge.resilience import ChaosEngine, CircuitBreaker, compute_backoff_ms

    now = [100.0]
    cb = CircuitBreaker("payments", failure_threshold=2, recovery_sec=10.0, clock=lambda: now[0])
    assert cb.state == BreakerState.CLOSED
    cb.record_failure()
    assert cb.state == BreakerState.CLOSED
    cb.record_failure()
    assert cb.state == BreakerState.OPEN
    assert cb.allow_request() is False

    now[0] = 111.0
    assert cb.allow_request() is True
    assert cb.state == BreakerState.HALF_OPEN
    cb.record_success()
    assert cb.state == BreakerState.CLOSED

    b1 = compute_backoff_ms(attempt=1, base_ms=100, max_ms=2000, jitter_ratio=0.0)
    b3 = compute_backoff_ms(attempt=3, base_ms=100, max_ms=2000, jitter_ratio=0.0)
    assert b1 == 100.0
    assert b3 == 400.0

    chaos = ChaosEngine()
    chaos.set_rules([ChaosRule(node_id="charge", crash_worker_once=True, latency_ms=25)])
    dec1 = chaos.evaluate("run-1", "charge")
    dec2 = chaos.evaluate("run-1", "charge")
    assert dec1.crash_worker is True
    assert dec2.crash_worker is False
    assert dec1.latency_ms == 25


def test_cron_parser_next_fire():
    from flowforge.cron import next_cron_run, parse_cron

    spec = parse_cron("*/15 9-17 * * 1-5")
    base = datetime(2026, 11, 23, 9, 10, tzinfo=timezone.utc)  # Monday
    nxt = next_cron_run(spec, after=base)
    assert nxt == datetime(2026, 11, 23, 9, 15, tzinfo=timezone.utc)

    with pytest.raises(ValueError):
        parse_cron("99 * * * *")
