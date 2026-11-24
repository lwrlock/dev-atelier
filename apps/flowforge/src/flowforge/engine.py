"""Distributed-style async DAG workflow engine with worker leases, chaos recovery, and Sagas."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import time
import uuid
from typing import Any

from .graph import CompiledDAG, compile_dag, safe_eval_condition
from .executors import WorkerCrashSimulated, execute_task_step
from .models import ChaosRule, RunStatus, TaskSpec, TaskStatus, TERMINAL_TASK_STATES, TriggerRule
from .resilience import ChaosEngine, CircuitBreakerRegistry, TokenBucketLimiter, compute_backoff_ms
from .storage import FlowStore
from .templates import builtin_workflows


class TelemetryHub:
    """Non-blocking WebSocket broadcast hub for live DAG & worker telemetry."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass


@dataclass(slots=True)
class WorkerState:
    worker_id: str
    status: str = "IDLE"  # IDLE | BUSY | RECOVERING
    current_run_id: str | None = None
    current_node_id: str | None = None
    lease_expires_at: float | None = None
    tasks_completed: int = 0
    crashes_survived: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class QueuedWorkItem:
    run_id: str
    node_id: str
    attempt: int
    queued_at: float
    phase: str = "exec"  # 'exec' | 'compensate'
    done_fut: asyncio.Future[tuple[TaskStatus, dict[str, Any], str | None, bool]] | None = None


class _ActiveRun:
    def __init__(
        self,
        *,
        run_id: str,
        compiled: CompiledDAG,
        params: dict[str, Any],
        chaos_rules: list[ChaosRule],
        started_wall: float,
    ) -> None:
        self.run_id = run_id
        self.compiled = compiled
        self.params = params
        self.chaos_rules = chaos_rules
        self.started_wall = started_wall
        self.states: dict[str, TaskStatus] = {nid: TaskStatus.PENDING for nid in compiled.nodes_by_id}
        self.attempts: dict[str, int] = {nid: 0 for nid in compiled.nodes_by_id}
        self.outputs: dict[str, dict[str, Any]] = {}
        self.saga_aborted = False
        self.cancelled = False
        self.done_event = asyncio.Event()
        self.result_summary: dict[str, Any] = {}
        self.limiter = TokenBucketLimiter(rate_per_sec=compiled.workflow.rate_limit_rps, burst=30.0)


class WorkflowEngine:
    """Async multi-worker DAG orchestrator with leases, chaos injection, Sagas & DLQ."""

    def __init__(
        self,
        *,
        store: FlowStore,
        worker_count: int = 6,
        lease_ttl_sec: float = 4.0,
    ) -> None:
        self.store = store
        self.worker_count = max(1, min(worker_count, 24))
        self.lease_ttl_sec = lease_ttl_sec
        self.hub = TelemetryHub()
        self.chaos = ChaosEngine()
        self.breakers = CircuitBreakerRegistry(failure_threshold=3, recovery_sec=10.0)
        self.workers: dict[str, WorkerState] = {
            f"worker-{i + 1:02d}": WorkerState(worker_id=f"worker-{i + 1:02d}")
            for i in range(self.worker_count)
        }
        self._queue: asyncio.Queue[QueuedWorkItem] = asyncio.Queue()
        self._active_runs: dict[str, _ActiveRun] = {}
        self._worker_tasks: list[asyncio.Task] = []
        self._running = False
        self.completed_tasks_total = 0
        self.crashes_recovered_total = 0

        # Ensure built-in workflows exist on fresh database
        existing_ids = {w["id"] for w in self.store.list_workflows()}
        for wf in builtin_workflows():
            if wf.id not in existing_ids:
                self.store.save_workflow(wf)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_tasks = [
            asyncio.create_task(self._worker_loop(wid)) for wid in sorted(self.workers)
        ]

    async def stop(self) -> None:
        self._running = False
        for t in self._worker_tasks:
            t.cancel()
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
        self._worker_tasks.clear()

    def _emit(
        self,
        run_id: str,
        node_id: str,
        event_type: str,
        state_from: str,
        state_to: str,
        *,
        worker_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> int:
        seq = self.store.append_event(
            run_id, node_id, event_type, state_from, state_to, worker_id=worker_id, detail=detail
        )
        self.hub.publish(
            {
                "type": "run_event",
                "event": {
                    "run_id": run_id,
                    "seq": seq,
                    "ts": time.time(),
                    "node_id": node_id,
                    "event_type": event_type,
                    "state_from": state_from,
                    "state_to": state_to,
                    "worker_id": worker_id,
                    "detail": detail or {},
                },
            }
        )
        return seq

    def _publish_workers(self) -> None:
        self.hub.publish(
            {
                "type": "workers_tick",
                "workers": [w.to_dict() for w in self.workers.values()],
                "breakers": self.breakers.list_snapshots(),
            }
        )

    async def _worker_loop(self, worker_id: str) -> None:
        w = self.workers[worker_id]
        while self._running:
            try:
                item = await self._queue.get()
            except asyncio.CancelledError:
                break

            run = self._active_runs.get(item.run_id)
            if run is None or run.cancelled:
                if item.done_fut and not item.done_fut.done():
                    item.done_fut.set_result((TaskStatus.SKIPPED, {}, "Cancelled", False))
                self._queue.task_done()
                continue

            node = run.compiled.nodes_by_id[item.node_id]
            await run.limiter.acquire()
            now = time.time()
            w.status = "BUSY"
            w.current_run_id = item.run_id
            w.current_node_id = item.node_id
            w.lease_expires_at = now + self.lease_ttl_sec
            self._publish_workers()

            prev_state = run.states[item.node_id].value
            target_state = TaskStatus.COMPENSATING.value if item.phase == "compensate" else TaskStatus.RUNNING.value
            run.states[item.node_id] = TaskStatus(target_state)
            self._emit(
                item.run_id,
                item.node_id,
                "SAGA_COMPENSATING" if item.phase == "compensate" else "TASK_STARTED",
                prev_state,
                target_state,
                worker_id=worker_id,
                detail={"attempt": item.attempt, "phase": item.phase},
            )

            upstream = {
                e.source: run.outputs.get(e.source, {})
                for e in run.compiled.parents[item.node_id]
            }
            attempt_row_id = self.store.record_attempt_start(
                run_id=item.run_id,
                node_id=item.node_id,
                attempt=item.attempt,
                phase=item.phase,
                worker_id=worker_id,
                queued_at=item.queued_at,
                started_at=now,
                input_data={"params": run.params, "upstream": upstream, "payload": node.payload},
            )

            t0 = time.monotonic()
            chaos_hit = False
            try:
                if item.phase == "exec":
                    breaker = self.breakers.get(node.resource_group)
                    if not breaker.allow_request():
                        raise RuntimeError(f"CircuitBreaker OPEN for '{node.resource_group}'")

                    dec = self.chaos.evaluate(item.run_id, item.node_id)
                    if dec.latency_ms > 0:
                        chaos_hit = True
                        await asyncio.sleep(min(2.0, dec.latency_ms / 1000.0))
                    if dec.crash_worker:
                        chaos_hit = True
                        raise WorkerCrashSimulated(f"Simulated worker crash on {worker_id}")
                    if dec.force_fail:
                        chaos_hit = True
                        raise RuntimeError(f"Chaos injected fault on node '{item.node_id}'")

                handler = node.compensate_handler if item.phase == "compensate" else node.handler
                res = await execute_task_step(
                    node,
                    handler_name=handler or "sim_compute",
                    upstream_outputs=upstream,
                    run_params=run.params,
                )
                dur_ms = (time.monotonic() - t0) * 1000.0
                if item.phase == "exec":
                    self.breakers.get(node.resource_group).record_success()
                end_state = TaskStatus.COMPENSATED if item.phase == "compensate" else TaskStatus.COMPLETED
                self.store.finish_attempt(
                    attempt_row_id,
                    status=end_state,
                    ended_at=time.time(),
                    duration_ms=dur_ms,
                    output_data=res.get("output", {}),
                    stdout_text=res.get("stdout", ""),
                    chaos_injected=chaos_hit,
                )
                w.tasks_completed += 1
                self.completed_tasks_total += 1
                if item.done_fut and not item.done_fut.done():
                    item.done_fut.set_result((end_state, res.get("output", {}), None, chaos_hit))

            except WorkerCrashSimulated as exc:
                dur_ms = (time.monotonic() - t0) * 1000.0
                w.status = "RECOVERING"
                w.crashes_survived += 1
                self.crashes_recovered_total += 1
                self.store.finish_attempt(
                    attempt_row_id,
                    status=TaskStatus.FAILED,
                    ended_at=time.time(),
                    duration_ms=dur_ms,
                    error_msg=str(exc),
                    chaos_injected=True,
                )
                self._emit(
                    item.run_id,
                    item.node_id,
                    "WORKER_CRASHED",
                    target_state,
                    TaskStatus.RETRYING.value,
                    worker_id=worker_id,
                    detail={"reason": str(exc)},
                )
                await asyncio.sleep(0.04)
                if item.done_fut and not item.done_fut.done():
                    item.done_fut.set_result((TaskStatus.RETRYING, {}, str(exc), True))

            except Exception as exc:
                dur_ms = (time.monotonic() - t0) * 1000.0
                if item.phase == "exec":
                    self.breakers.get(node.resource_group).record_failure()
                self.store.finish_attempt(
                    attempt_row_id,
                    status=TaskStatus.FAILED,
                    ended_at=time.time(),
                    duration_ms=dur_ms,
                    error_msg=str(exc),
                    chaos_injected=chaos_hit,
                )
                if item.done_fut and not item.done_fut.done():
                    item.done_fut.set_result((TaskStatus.FAILED, {}, str(exc), chaos_hit))

            finally:
                w.status = "IDLE"
                w.current_run_id = None
                w.current_node_id = None
                w.lease_expires_at = None
                self._publish_workers()
                self._queue.task_done()

    async def run_workflow(
        self,
        workflow_id: str,
        *,
        params: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        trigger_type: str = "manual",
        wait: bool = False,
    ) -> dict[str, Any]:
        wf = self.store.get_workflow(workflow_id)
        if wf is None:
            raise KeyError(f"Workflow not found: {workflow_id}")
        compiled = compile_dag(wf)
        run_id = uuid.uuid4().hex[:12]
        active_rules = self.chaos.get_rules()
        self.store.create_run(
            run_id=run_id,
            workflow_id=workflow_id,
            idempotency_key=idempotency_key,
            trigger_type=trigger_type,
            params=params or {},
            chaos_rules=[r.model_dump() for r in active_rules],
        )
        run = _ActiveRun(
            run_id=run_id,
            compiled=compiled,
            params=params or {},
            chaos_rules=active_rules,
            started_wall=time.time(),
        )
        self._active_runs[run_id] = run
        self.hub.publish({"type": "run_started", "run_id": run_id, "workflow_id": workflow_id})

        orch = asyncio.create_task(self._orchestrate_run(run))
        if wait:
            await orch
            return run.result_summary
        return {"run_id": run_id, "workflow_id": workflow_id, "status": RunStatus.RUNNING.value}

    def _node_ready_or_skip(self, run: _ActiveRun, nid: str) -> str:
        """Return 'RUN', 'SKIP', or 'WAIT' given upstream parent states & edge predicates."""
        parents = run.compiled.parents[nid]
        if not parents:
            return "RUN"
        parent_states = [run.states[e.source] for e in parents]
        if not all(st in TERMINAL_TASK_STATES for st in parent_states):
            # Optimization: ONE_SUCCESS can fire as soon as any satisfied edge succeeds
            node = run.compiled.nodes_by_id[nid]
            if node.trigger_rule == TriggerRule.ONE_SUCCESS:
                for edge in parents:
                    if run.states[edge.source] == TaskStatus.COMPLETED:
                        if safe_eval_condition(edge.condition, {"upstream": run.outputs.get(edge.source, {})}):
                            return "RUN"
            return "WAIT"

        node = run.compiled.nodes_by_id[nid]
        satisfied_edges = []
        for edge in parents:
            st = run.states[edge.source]
            if st == TaskStatus.COMPLETED:
                cond_ok = safe_eval_condition(edge.condition, {"upstream": run.outputs.get(edge.source, {})})
                if cond_ok:
                    satisfied_edges.append(edge)

        rule = node.trigger_rule
        if rule == TriggerRule.ALL_SUCCESS:
            if all(run.states[e.source] == TaskStatus.COMPLETED for e in parents) and len(satisfied_edges) == len(parents):
                return "RUN"
            return "SKIP"
        if rule == TriggerRule.ONE_SUCCESS:
            return "RUN" if satisfied_edges else "SKIP"
        if rule == TriggerRule.ALL_DONE:
            return "RUN" if any(run.states[e.source] != TaskStatus.SKIPPED for e in parents) else "SKIP"
        if rule == TriggerRule.ONE_FAILED:
            if any(run.states[e.source] in {TaskStatus.FAILED, TaskStatus.DEAD_LETTERED} for e in parents):
                return "RUN"
            return "SKIP"
        return "SKIP"

    async def _run_node_with_retries(self, run: _ActiveRun, nid: str) -> None:
        node = run.compiled.nodes_by_id[nid]
        attempt = 0
        while not run.cancelled and not run.saga_aborted:
            attempt += 1
            run.attempts[nid] = attempt
            loop = asyncio.get_running_loop()
            fut: asyncio.Future = loop.create_future()
            prev = run.states[nid].value
            run.states[nid] = TaskStatus.QUEUED
            self._emit(run.run_id, nid, "TASK_QUEUED", prev, TaskStatus.QUEUED.value, detail={"attempt": attempt})

            await self._queue.put(
                QueuedWorkItem(
                    run_id=run.run_id, node_id=nid, attempt=attempt, queued_at=time.time(), done_fut=fut
                )
            )
            status, output, err_msg, _ = await fut

            if status == TaskStatus.COMPLETED:
                run.states[nid] = TaskStatus.COMPLETED
                run.outputs[nid] = output
                self._emit(run.run_id, nid, "TASK_COMPLETED", TaskStatus.RUNNING.value, TaskStatus.COMPLETED.value)
                return

            if status == TaskStatus.RETRYING:
                # Worker crash recovery does not consume business failure retries unfairly, cap at max_retries + 2
                if attempt <= node.max_retries + 2:
                    await asyncio.sleep(0.02)
                    continue

            if attempt <= node.max_retries:
                backoff_ms = compute_backoff_ms(attempt=attempt, base_ms=node.retry_base_ms, max_ms=1500)
                run.states[nid] = TaskStatus.RETRYING
                self._emit(
                    run.run_id,
                    nid,
                    "TASK_RETRYING",
                    TaskStatus.RUNNING.value,
                    TaskStatus.RETRYING.value,
                    detail={"attempt": attempt, "backoff_ms": backoff_ms, "error": err_msg},
                )
                await asyncio.sleep(min(0.4, backoff_ms / 1000.0))
                continue

            # Terminal failure -> Dead-Letter Queue
            run.states[nid] = TaskStatus.DEAD_LETTERED
            self._emit(
                run.run_id,
                nid,
                "TASK_DEAD_LETTERED",
                TaskStatus.RUNNING.value,
                TaskStatus.DEAD_LETTERED.value,
                detail={"attempt": attempt, "error": err_msg},
            )
            self.store.enqueue_dlq(
                run.run_id,
                run.compiled.workflow.id,
                nid,
                attempt,
                err_msg or "Max retries exhausted",
                node.payload,
            )
            if run.compiled.workflow.saga_enabled:
                run.saga_aborted = True
            return

    async def _orchestrate_run(self, run: _ActiveRun) -> None:
        t0 = time.monotonic()
        in_flight: dict[str, asyncio.Task] = {}
        try:
            while not run.cancelled:
                # Reap finished node tasks
                done_ids = [nid for nid, t in in_flight.items() if t.done()]
                for nid in done_ids:
                    in_flight.pop(nid).result()

                if run.saga_aborted:
                    break

                # Schedule newly ready nodes or mark unreachable branches SKIPPED
                progressed = False
                for nid in run.compiled.topological_order:
                    if run.states[nid] != TaskStatus.PENDING or nid in in_flight:
                        continue
                    decision = self._node_ready_or_skip(run, nid)
                    if decision == "RUN":
                        in_flight[nid] = asyncio.create_task(self._run_node_with_retries(run, nid))
                        progressed = True
                    elif decision == "SKIP":
                        run.states[nid] = TaskStatus.SKIPPED
                        self._emit(run.run_id, nid, "TASK_SKIPPED", TaskStatus.PENDING.value, TaskStatus.SKIPPED.value)
                        progressed = True

                all_done = all(st in TERMINAL_TASK_STATES for st in run.states.values()) and not in_flight
                if all_done:
                    break
                if not progressed and in_flight:
                    await asyncio.wait(in_flight.values(), return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(0.01)

            if in_flight:
                await asyncio.gather(*in_flight.values(), return_exceptions=True)

            saga_triggered = False
            if run.saga_aborted and not run.cancelled:
                saga_triggered = True
                # Mark remaining PENDING nodes SKIPPED
                for nid in run.compiled.topological_order:
                    if run.states[nid] == TaskStatus.PENDING:
                        run.states[nid] = TaskStatus.SKIPPED
                        self._emit(run.run_id, nid, "TASK_SKIPPED", TaskStatus.PENDING.value, TaskStatus.SKIPPED.value)
                # Reverse topological Saga rollback for completed nodes with compensate_handler
                for nid in reversed(run.compiled.topological_order):
                    node = run.compiled.nodes_by_id[nid]
                    if run.states[nid] == TaskStatus.COMPLETED and node.compensate_handler:
                        loop = asyncio.get_running_loop()
                        fut: asyncio.Future = loop.create_future()
                        await self._queue.put(
                            QueuedWorkItem(
                                run_id=run.run_id,
                                node_id=nid,
                                attempt=1,
                                phase="compensate",
                                queued_at=time.time(),
                                done_fut=fut,
                            )
                        )
                        comp_status, _, _, _ = await fut
                        run.states[nid] = comp_status
                        self._emit(
                            run.run_id,
                            nid,
                            "SAGA_COMPENSATED",
                            TaskStatus.COMPENSATING.value,
                            comp_status.value,
                        )
                final_status = RunStatus.COMPENSATED
            elif run.cancelled:
                final_status = RunStatus.CANCELLED
            elif any(st in {TaskStatus.FAILED, TaskStatus.DEAD_LETTERED} for st in run.states.values()):
                final_status = RunStatus.FAILED
            else:
                final_status = RunStatus.COMPLETED

            dur_ms = round((time.monotonic() - t0) * 1000.0, 2)
            self.store.finish_run(
                run.run_id, final_status, saga_triggered=saga_triggered, duration_ms=dur_ms
            )
            run.result_summary = {
                "run_id": run.run_id,
                "workflow_id": run.compiled.workflow.id,
                "status": final_status.value,
                "saga_triggered": saga_triggered,
                "duration_ms": dur_ms,
            }
            self.hub.publish({"type": "run_finished", **run.result_summary})
        finally:
            run.done_event.set()
            self._active_runs.pop(run.run_id, None)

    def cancel_run(self, run_id: str) -> bool:
        run = self._active_runs.get(run_id)
        if run is None:
            return False
        run.cancelled = True
        return True

    async def replay_dlq_item(self, dlq_id: int) -> dict[str, Any]:
        items = self.store.list_dlq(unresolved_only=False, limit=200)
        item = next((x for x in items if x["id"] == dlq_id), None)
        if item is None:
            raise KeyError(f"DLQ item {dlq_id} not found")
        self.store.mark_dlq_resolved(dlq_id)
        # Re-launch fresh run of the workflow safely (clean breaker first)
        self.breakers.reset_all()
        return await self.run_workflow(
            item["workflow_id"],
            params={"replayed_from_dlq": dlq_id, "target_node": item["node_id"]},
            trigger_type="dlq_replay",
            wait=True,
        )

    async def run_burst_benchmark(self, workflow_id: str, *, count: int = 12) -> dict[str, Any]:
        """Fire N concurrent workflow runs and measure throughput + latency percentiles."""
        n = max(1, min(int(count), 50))
        t0 = time.monotonic()
        results = await asyncio.gather(
            *[
                self.run_workflow(workflow_id, params={"burst_idx": i}, trigger_type="benchmark", wait=True)
                for i in range(n)
            ]
        )
        wall_sec = max(0.001, time.monotonic() - t0)
        durs = sorted(float(r.get("duration_ms") or 0.0) for r in results)
        p50 = durs[len(durs) // 2]
        p95 = durs[min(len(durs) - 1, int(len(durs) * 0.95))]
        counts: dict[str, int] = {}
        for r in results:
            st = r["status"]
            counts[st] = counts.get(st, 0) + 1
        return {
            "workflow_id": workflow_id,
            "count": n,
            "wall_sec": round(wall_sec, 3),
            "runs_per_sec": round(n / wall_sec, 2),
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "status_counts": counts,
            "crashes_recovered_total": self.crashes_recovered_total,
        }
