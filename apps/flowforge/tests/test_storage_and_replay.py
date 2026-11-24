from pathlib import Path
import threading
import pytest


def test_storage_workflows_idempotency_and_time_travel_replay(tmp_path: Path):
    from flowforge.models import EdgeSpec, RunStatus, TaskSpec, TaskStatus, WorkflowSpec
    from flowforge.storage import DuplicateRunError, FlowStore

    store = FlowStore(tmp_path / "flowforge.db")
    wf = WorkflowSpec(
        id="wf-saga",
        name="Order Saga",
        nodes=[
            TaskSpec(id="reserve", label="Reserve"),
            TaskSpec(id="charge", label="Charge", compensate_handler="refund"),
        ],
        edges=[EdgeSpec(source="reserve", target="charge")],
    )
    store.save_workflow(wf)
    assert store.get_workflow("wf-saga").name == "Order Saga"

    store.create_run(
        run_id="run-1",
        workflow_id="wf-saga",
        idempotency_key="order-99",
        trigger_type="api",
        params={"amount": 42},
        chaos_rules=[],
    )
    with pytest.raises(DuplicateRunError) as exc:
        store.create_run(
            run_id="run-2",
            workflow_id="wf-saga",
            idempotency_key="order-99",
            trigger_type="api",
            params={},
            chaos_rules=[],
        )
    assert exc.value.existing_run_id == "run-1"

    # Append deterministic timeline events
    s1 = store.append_event("run-1", "reserve", "TASK_QUEUED", "PENDING", "QUEUED")
    s2 = store.append_event("run-1", "reserve", "TASK_STARTED", "QUEUED", "RUNNING", worker_id="worker-01")
    s3 = store.append_event("run-1", "reserve", "TASK_COMPLETED", "RUNNING", "COMPLETED", worker_id=None)
    s4 = store.append_event("run-1", "charge", "TASK_STARTED", "QUEUED", "RUNNING", worker_id="worker-02")
    s5 = store.append_event("run-1", "charge", "TASK_FAILED", "RUNNING", "FAILED", worker_id="worker-02")
    s6 = store.append_event("run-1", "reserve", "SAGA_COMPENSATED", "COMPLETED", "COMPENSATED", worker_id="worker-01")

    assert [s1, s2, s3, s4, s5, s6] == [1, 2, 3, 4, 5, 6]

    snap_mid = store.reconstruct_run_snapshot("run-1", up_to_seq=3)
    assert snap_mid["node_states"]["reserve"] == TaskStatus.COMPLETED.value
    assert snap_mid["node_states"]["charge"] == TaskStatus.PENDING.value
    assert snap_mid["active_workers"] == {}

    snap_at_4 = store.reconstruct_run_snapshot("run-1", up_to_seq=4)
    assert snap_at_4["node_states"]["charge"] == TaskStatus.RUNNING.value
    assert snap_at_4["active_workers"]["worker-02"] == "charge"

    snap_end = store.reconstruct_run_snapshot("run-1", up_to_seq=6)
    assert snap_end["node_states"]["reserve"] == TaskStatus.COMPENSATED.value
    assert snap_end["node_states"]["charge"] == TaskStatus.FAILED.value

    store.finish_run("run-1", RunStatus.COMPENSATED, saga_triggered=True, duration_ms=123.4)
    runs = store.list_runs()
    assert runs[0]["status"] == RunStatus.COMPENSATED.value
    assert runs[0]["saga_triggered"] is True


def test_storage_dlq_and_concurrent_events(tmp_path: Path):
    from flowforge.models import TaskSpec, WorkflowSpec
    from flowforge.storage import FlowStore

    store = FlowStore(tmp_path / "dlq.db")
    store.save_workflow(WorkflowSpec(id="w1", name="W1", nodes=[TaskSpec(id="a", label="A")]))
    store.create_run("r1", "w1", None, "manual", {}, [])

    dlq_id = store.enqueue_dlq("r1", "w1", "a", 3, "timeout", {"x": 1})
    items = store.list_dlq(unresolved_only=True)
    assert len(items) == 1
    assert items[0]["id"] == dlq_id
    assert store.mark_dlq_resolved(dlq_id) is True
    assert store.list_dlq(unresolved_only=True) == []

    errs: list[Exception] = []

    def writer(i: int):
        try:
            for k in range(12):
                store.append_event("r1", "a", f"EVT_{i}_{k}", "PENDING", "RUNNING")
        except Exception as exc:  # pragma: no cover
            errs.append(exc)

    threads = [threading.Thread(target=writer, args=(idx,)) for idx in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errs
    evts = store.list_events("r1", limit=100)
    assert len(evts) == 48
    assert [e["seq"] for e in evts] == list(range(1, 49))
