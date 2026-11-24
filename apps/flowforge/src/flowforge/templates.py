"""Pre-built real-world production DAG workflow templates."""

from __future__ import annotations

from .models import EdgeSpec, TaskSpec, TriggerRule, WorkflowSpec


def builtin_workflows() -> list[WorkflowSpec]:
    return [
        WorkflowSpec(
            id="order-payment-saga",
            name="Order & Payment Distributed Saga",
            description="E-commerce checkout with risk scoring, conditional manual review, idempotent charge & reverse-topological Saga rollback.",
            saga_enabled=True,
            nodes=[
                TaskSpec(id="validate", label="Validate Cart", handler="sim_compute", estimated_ms=45, resource_group="gateway"),
                TaskSpec(id="reserve", label="Reserve Inventory", handler="inventory_reserve", compensate_handler="inventory_release", estimated_ms=70, resource_group="inventory"),
                TaskSpec(id="fraud", label="Fraud ML Score", handler="fraud_score", estimated_ms=95, resource_group="risk", payload={"score": 0.14}),
                TaskSpec(id="charge", label="Capture Payment", handler="payment_charge", compensate_handler="payment_refund", estimated_ms=130, resource_group="payments", max_retries=2),
                TaskSpec(id="manual_hold", label="Compliance Hold", handler="sim_compute", estimated_ms=80, resource_group="risk"),
                TaskSpec(id="fulfill", label="Dispatch Fulfillment", handler="sim_compute", estimated_ms=65, resource_group="shipping", trigger_rule=TriggerRule.ALL_SUCCESS),
                TaskSpec(id="notify", label="Send Receipt", handler="sim_compute", estimated_ms=35, resource_group="comms", trigger_rule=TriggerRule.ONE_SUCCESS),
            ],
            edges=[
                EdgeSpec(source="validate", target="reserve"),
                EdgeSpec(source="reserve", target="fraud"),
                EdgeSpec(source="fraud", target="charge", condition="upstream.risk == 'low'"),
                EdgeSpec(source="fraud", target="manual_hold", condition="upstream.risk == 'high'"),
                EdgeSpec(source="charge", target="fulfill"),
                EdgeSpec(source="fulfill", target="notify"),
                EdgeSpec(source="manual_hold", target="notify"),
            ],
        ),
        WorkflowSpec(
            id="ml-hyperparam-sweep",
            name="Parallel ML Hyperparameter Sweep",
            description="Dataset prep -> 3-way parallel training fan-out across worker lanes -> champion model gate -> canary promotion.",
            saga_enabled=True,
            nodes=[
                TaskSpec(id="ingest", label="Fetch Feature Store", handler="sim_compute", estimated_ms=60, resource_group="storage"),
                TaskSpec(id="split", label="Stratified Split", handler="sim_compute", estimated_ms=50, resource_group="cpu"),
                TaskSpec(id="train_lr1", label="Train LR=1e-4", handler="ml_train_fold", estimated_ms=150, resource_group="gpu", payload={"lr": 0.0001}),
                TaskSpec(id="train_lr2", label="Train LR=1e-3", handler="ml_train_fold", estimated_ms=190, resource_group="gpu", payload={"lr": 0.001}),
                TaskSpec(id="train_lr3", label="Train LR=1e-2", handler="ml_train_fold", estimated_ms=130, resource_group="gpu", payload={"lr": 0.01}),
                TaskSpec(id="select", label="Champion Eval", handler="ml_select_champion", estimated_ms=55, resource_group="eval"),
                TaskSpec(id="deploy", label="Canary Model Serve", handler="canary_deploy", compensate_handler="canary_rollback", estimated_ms=85, resource_group="serving", payload={"weight_pct": 15}),
            ],
            edges=[
                EdgeSpec(source="ingest", target="split"),
                EdgeSpec(source="split", target="train_lr1"),
                EdgeSpec(source="split", target="train_lr2"),
                EdgeSpec(source="split", target="train_lr3"),
                EdgeSpec(source="train_lr1", target="select"),
                EdgeSpec(source="train_lr2", target="select"),
                EdgeSpec(source="train_lr3", target="select"),
                EdgeSpec(source="select", target="deploy"),
            ],
        ),
        WorkflowSpec(
            id="canary-zero-downtime",
            name="Zero-Downtime CI/CD Canary",
            description="Parallel build & security scan -> integration -> progressive 10%/50%/100% rollout with automatic rollback safety.",
            saga_enabled=True,
            nodes=[
                TaskSpec(id="build", label="Build Artifact", handler="sim_compute", estimated_ms=90, resource_group="ci"),
                TaskSpec(id="unit_test", label="Unit Test Suite", handler="sim_compute", estimated_ms=80, resource_group="ci"),
                TaskSpec(id="sec_scan", label="SAST & SBOM Scan", handler="sim_compute", estimated_ms=110, resource_group="security"),
                TaskSpec(id="canary_10", label="Canary 10%", handler="canary_deploy", compensate_handler="canary_rollback", estimated_ms=70, resource_group="k8s", payload={"weight_pct": 10}),
                TaskSpec(id="slo_probe", label="SLO Error Budget Probe", handler="sim_compute", estimated_ms=95, resource_group="observability"),
                TaskSpec(id="promote", label="Promote 100%", handler="canary_deploy", compensate_handler="canary_rollback", estimated_ms=60, resource_group="k8s", payload={"weight_pct": 100}),
            ],
            edges=[
                EdgeSpec(source="build", target="unit_test"),
                EdgeSpec(source="build", target="sec_scan"),
                EdgeSpec(source="unit_test", target="canary_10"),
                EdgeSpec(source="sec_scan", target="canary_10"),
                EdgeSpec(source="canary_10", target="slo_probe"),
                EdgeSpec(source="slo_probe", target="promote"),
            ],
        ),
        WorkflowSpec(
            id="fin-lakehouse-etl",
            name="Financial Lakehouse ETL",
            description="Parallel market ticks & FX ingest -> normalize -> VaR anomaly check -> parquet partition commit.",
            saga_enabled=False,
            nodes=[
                TaskSpec(id="ticks", label="Ingest Market Ticks", handler="sim_compute", estimated_ms=85, resource_group="feed"),
                TaskSpec(id="fx", label="Ingest FX Rates", handler="sim_compute", estimated_ms=55, resource_group="feed"),
                TaskSpec(id="join", label="As-Of Temporal Join", handler="sim_compute", estimated_ms=110, resource_group="spark"),
                TaskSpec(id="var_gate", label="VaR Quality Gate", handler="sim_compute", estimated_ms=70, resource_group="risk"),
                TaskSpec(id="publish", label="Commit Lakehouse", handler="sim_compute", estimated_ms=50, resource_group="storage"),
            ],
            edges=[
                EdgeSpec(source="ticks", target="join"),
                EdgeSpec(source="fx", target="join"),
                EdgeSpec(source="join", target="var_gate"),
                EdgeSpec(source="var_gate", target="publish"),
            ],
        ),
    ]
