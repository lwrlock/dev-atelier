import asyncio
from pathlib import Path
import pytest


def test_engine_diamond_conditional_branch_and_skip(tmp_path: Path):
    from flowforge.engine import WorkflowEngine
    from flowforge.models import EdgeSpec, RunStatus, TaskSpec, TaskStatus, TriggerRule, WorkflowSpec
    from flowforge.storage import FlowStore

    store = FlowStore(tmp_path / "branch.db")
    wf = WorkflowSpec(
        id="wf-branch",
        name="Conditional Branch",
        nodes=[
            TaskSpec(id="score", label="Score", handler="fraud_score", estimated_ms=25, payload={"score": 0.15}),
            TaskSpec(id="fast_path", label="Approve", handler="sim_compute", estimated_ms=20),
            TaskSpec(id="manual_review", label="Manual", handler="sim_compute", estimated_ms=20),
            TaskSpec(
                id="finalize",
                label="Finalize",
                handler="sim_compute",
                estimated_ms=15,
                trigger_rule=TriggerRule.ONE_SUCCESS,
            ),
        ],
        edges=[
            EdgeSpec(source="score", target="fast_path", condition="upstream.risk == 'low'"),
            EdgeSpec(source="score", target="manual_review", condition="upstream.risk == 'high'"),
            EdgeSpec(source="fast_path", target="finalize"),
            EdgeSpec(source="manual_review", target="finalize"),
        ],
    )
    store.save_workflow(wf)

    async def go():
        engine = WorkflowEngine(store=store, worker_count=4)
        await engine.start()
        try:
            res = await engine.run_workflow("wf-branch", wait=True)
            assert res["status"] == RunStatus.COMPLETED.value
            detail = store.get_run(res["run_id"])
            snap = store.reconstruct_run_snapshot(res["run_id"])
            assert snap["node_states"]["fast_path"] == TaskStatus.COMPLETED.value
            assert snap["node_states"]["manual_review"] == TaskStatus.SKIPPED.value
            assert snap["node_states"]["finalize"] == TaskStatus.COMPLETED.value
            assert len(detail["events"]) >= 6
        finally:
            await engine.stop()

    asyncio.run(go())


def test_engine_worker_crash_recovery_via_requeue(tmp_path: Path):
    from flowforge.engine import WorkflowEngine
    from flowforge.models import ChaosRule, EdgeSpec, RunStatus, TaskSpec, WorkflowSpec
    from flowforge.storage import FlowStore

    store = FlowStore(tmp_path / "crash.db")
    wf = WorkflowSpec(
        id="wf-crash",
        name="Crash Recovery",
        nodes=[
            TaskSpec(id="ingest", label="Ingest", handler="sim_compute", estimated_ms=20),
            TaskSpec(id="transform", label="Transform", handler="sim_compute", estimated_ms=20, max_retries=2, retry_base_ms=15),
        ],
        edges=[EdgeSpec(source="ingest", target="transform")],
    )
    store.save_workflow(wf)

    async def go():
        engine = WorkflowEngine(store=store, worker_count=3)
        engine.chaos.set_rules([ChaosRule(node_id="transform", crash_worker_once=True)])
        await engine.start()
        try:
            res = await engine.run_workflow("wf-crash", wait=True)
            assert res["status"] == RunStatus.COMPLETED.value
            detail = store.get_run(res["run_id"])
            assert any(e["event_type"] == "WORKER_CRASHED" for e in detail["events"])
        finally:
            await engine.stop()

    asyncio.run(go())


def test_engine_saga_reverse_compensation_and_dlq(tmp_path: Path):
    from flowforge.engine import WorkflowEngine
    from flowforge.models import ChaosRule, EdgeSpec, RunStatus, TaskSpec, TaskStatus, WorkflowSpec
    from flowforge.storage import FlowStore

    store = FlowStore(tmp_path / "saga.db")
    wf = WorkflowSpec(
        id="wf-saga",
        name="Payment Saga",
        saga_enabled=True,
        nodes=[
            TaskSpec(id="reserve", label="Reserve Inventory", handler="inventory_reserve", compensate_handler="inventory_release", estimated_ms=15),
            TaskSpec(id="fraud", label="Fraud Check", handler="fraud_score", estimated_ms=15, payload={"score": 0.1}),
            TaskSpec(id="charge", label="Charge Card", handler="payment_charge", compensate_handler="payment_refund", estimated_ms=15, max_retries=1, retry_base_ms=10),
            TaskSpec(id="ship", label="Ship Order", handler="sim_compute", estimated_ms=15),
        ],
        edges=[
            EdgeSpec(source="reserve", target="fraud"),
            EdgeSpec(source="fraud", target="charge"),
            EdgeSpec(source="charge", target="ship"),
        ],
    )
    store.save_workflow(wf)

    async def go():
        engine = WorkflowEngine(store=store, worker_count=4)
        engine.chaos.set_rules([ChaosRule(node_id="charge", fail_prob=1.0)])
        await engine.start()
        try:
            res = await engine.run_workflow("wf-saga", wait=True)
            assert res["status"] == RunStatus.COMPENSATED.value
            assert res["saga_triggered"] is True
            snap = store.reconstruct_run_snapshot(res["run_id"])
            assert snap["node_states"]["reserve"] == TaskStatus.COMPENSATED.value
            assert snap["node_states"]["charge"] == TaskStatus.DEAD_LETTERED.value
            assert snap["node_states"]["ship"] == TaskStatus.SKIPPED.value
            dlq = store.list_dlq(unresolved_only=True)
            assert len(dlq) == 1
            assert dlq[0]["node_id"] == "charge"
        finally:
            await engine.stop()

    asyncio.run(go())


def test_shell_safe_executor_rejects_disallowed_commands():
    from flowforge.executors import SecurityViolationError, run_shell_safe

    async def go():
        with pytest.raises(SecurityViolationError):
            await run_shell_safe({"cmd": ["rm", "-rf", "/"]}, timeout_sec=1.0)
        with pytest.raises(SecurityViolationError):
            await run_shell_safe({"cmd": ["bash", "-c", "whoami"]}, timeout_sec=1.0)
        out = await run_shell_safe({"cmd": ["uname", "-s"]}, timeout_sec=2.0)
        assert "Linux" in out["stdout"]

    asyncio.run(go())
