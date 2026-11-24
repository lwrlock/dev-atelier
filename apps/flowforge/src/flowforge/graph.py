"""DAG validation, Tarjan SCC cycle detection, Sugiyama layout, CPM, and AST condition eval."""

from __future__ import annotations

import ast
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from .models import EdgeSpec, TaskSpec, WorkflowSpec


class CycleError(ValueError):
    """Raised when a workflow graph contains a cycle."""

    def __init__(self, cycle: list[str]) -> None:
        self.cycle = cycle
        super().__init__(f"Cycle detected in workflow graph: {' -> '.join(cycle)}")


class UnsafeExpressionError(ValueError):
    """Raised when an edge condition uses disallowed syntax or identifiers."""


@dataclass(slots=True)
class CPMMetrics:
    es_ms: int
    ef_ms: int
    ls_ms: int
    lf_ms: int
    slack_ms: int
    is_critical: bool


@dataclass(slots=True)
class CompiledDAG:
    workflow: WorkflowSpec
    nodes_by_id: dict[str, TaskSpec]
    parents: dict[str, list[EdgeSpec]]
    children: dict[str, list[EdgeSpec]]
    topological_order: list[str]
    layers: dict[str, int]
    positions: dict[str, tuple[float, float]]
    cpm: dict[str, CPMMetrics]
    critical_path: list[str]
    makespan_ms: int


def find_cycles(node_ids: list[str], edges: list[EdgeSpec]) -> list[list[str]]:
    """Run Tarjan's Strongly Connected Components to return all cycles cleanly."""
    adj: dict[str, list[str]] = defaultdict(list)
    self_loops: list[list[str]] = []
    for e in edges:
        if e.source == e.target:
            self_loops.append([e.source, e.target])
        adj[e.source].append(e.target)

    index = 0
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    cycles: list[list[str]] = list(self_loops)

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)

        for w in adj[v]:
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])

        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.remove(w)
                comp.append(w)
                if w == v:
                    break
            if len(comp) > 1:
                cycles.append(list(reversed(comp)))

    for nid in node_ids:
        if nid not in indices:
            strongconnect(nid)
    return cycles


def compile_dag(workflow: WorkflowSpec) -> CompiledDAG:
    """Validate DAG acyclicity, compute layered Sugiyama positions and Critical Path Method."""
    nodes_by_id = {n.id: n for n in workflow.nodes}
    for edge in workflow.edges:
        if edge.source not in nodes_by_id:
            raise ValueError(f"Edge source missing: {edge.source}")
        if edge.target not in nodes_by_id:
            raise ValueError(f"Edge target missing: {edge.target}")

    cycles = find_cycles(list(nodes_by_id.keys()), workflow.edges)
    if cycles:
        raise CycleError(cycles[0])

    parents: dict[str, list[EdgeSpec]] = {nid: [] for nid in nodes_by_id}
    children: dict[str, list[EdgeSpec]] = {nid: [] for nid in nodes_by_id}
    indeg: dict[str, int] = {nid: 0 for nid in nodes_by_id}
    for edge in workflow.edges:
        children[edge.source].append(edge)
        parents[edge.target].append(edge)
        indeg[edge.target] += 1

    # Kahn topological order
    q = deque([nid for nid, deg in indeg.items() if deg == 0])
    topo: list[str] = []
    while q:
        u = q.popleft()
        topo.append(u)
        for edge in children[u]:
            indeg[edge.target] -= 1
            if indeg[edge.target] == 0:
                q.append(edge.target)

    # Layer assignment (longest path from roots)
    layers: dict[str, int] = {}
    for u in topo:
        if not parents[u]:
            layers[u] = 0
        else:
            layers[u] = max(layers[e.source] + 1 for e in parents[u])

    by_layer: dict[int, list[str]] = defaultdict(list)
    for u in topo:
        by_layer[layers[u]].append(u)

    # Barycenter row ordering per layer to minimize edge crossings
    row_order: dict[str, float] = {}
    for layer_idx in sorted(by_layer):
        mems = by_layer[layer_idx]
        if layer_idx > 0:
            mems.sort(
                key=lambda nid: (
                    sum(row_order[e.source] for e in parents[nid]) / max(1, len(parents[nid])),
                    nid,
                )
            )
        for idx, nid in enumerate(mems):
            row_order[nid] = float(idx)

    # Assign clean canvas positions (preserves user x/y if explicitly provided)
    positions: dict[str, tuple[float, float]] = {}
    x_gap = 295.0
    y_gap = 138.0
    for layer_idx, mems in by_layer.items():
        count = len(mems)
        top_offset = 240.0 - ((count - 1) * y_gap) / 2.0
        for row_idx, nid in enumerate(mems):
            node = nodes_by_id[nid]
            px = node.x if node.x is not None else (80.0 + layer_idx * x_gap)
            py = node.y if node.y is not None else max(60.0, top_offset + row_idx * y_gap)
            positions[nid] = (round(px, 1), round(py, 1))

    # Critical Path Method (forward + backward pass)
    es: dict[str, int] = {}
    ef: dict[str, int] = {}
    for u in topo:
        es[u] = max((ef[e.source] for e in parents[u]), default=0)
        ef[u] = es[u] + nodes_by_id[u].estimated_ms

    makespan = max(ef.values(), default=0)
    ls: dict[str, int] = {}
    lf: dict[str, int] = {}
    for u in reversed(topo):
        lf[u] = min((ls[e.target] for e in children[u]), default=makespan)
        ls[u] = lf[u] - nodes_by_id[u].estimated_ms

    cpm: dict[str, CPMMetrics] = {}
    critical_path: list[str] = []
    for u in topo:
        slack = ls[u] - es[u]
        crit = slack == 0
        cpm[u] = CPMMetrics(
            es_ms=es[u], ef_ms=ef[u], ls_ms=ls[u], lf_ms=lf[u], slack_ms=slack, is_critical=crit
        )
        if crit:
            critical_path.append(u)

    return CompiledDAG(
        workflow=workflow,
        nodes_by_id=nodes_by_id,
        parents=parents,
        children=children,
        topological_order=topo,
        layers=layers,
        positions=positions,
        cpm=cpm,
        critical_path=critical_path,
        makespan_ms=makespan,
    )


_ALLOWED_CMP = {
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b,
    ast.NotIn: lambda a, b: a not in b,
}


def safe_eval_condition(expr: str | None, ctx: dict[str, Any]) -> bool:
    """Evaluate edge branch predicates safely via restricted AST traversal (never eval())."""
    if not expr or not expr.strip():
        return True
    try:
        tree = ast.parse(expr.strip(), mode="eval")
    except SyntaxError as exc:
        raise UnsafeExpressionError(f"Invalid condition syntax: {exc}") from exc

    def _eval(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (str, int, float, bool)) or node.value is None:
                return node.value
            raise UnsafeExpressionError("Disallowed literal type.")
        if isinstance(node, (ast.List, ast.Tuple)):
            return [_eval(elt) for elt in node.elts]
        if isinstance(node, ast.Name):
            if node.id.startswith("_"):
                raise UnsafeExpressionError(f"Disallowed identifier: {node.id}")
            if node.id not in ctx:
                return None
            return ctx[node.id]
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                raise UnsafeExpressionError(f"Disallowed dunder/private attribute: {node.attr}")
            base = _eval(node.value)
            if isinstance(base, dict):
                return base.get(node.attr)
            raise UnsafeExpressionError("Attribute traversal only permitted on dict contexts.")
        if isinstance(node, ast.Subscript):
            base = _eval(node.value)
            key = _eval(node.slice)
            if isinstance(base, dict):
                return base.get(key)
            if isinstance(base, (list, tuple)) and isinstance(key, int):
                return base[key]
            raise UnsafeExpressionError("Disallowed subscript target.")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(_eval(node.operand))
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(_eval(v)) for v in node.values)
            if isinstance(node.op, ast.Or):
                return any(bool(_eval(v)) for v in node.values)
        if isinstance(node, ast.Compare):
            left = _eval(node.left)
            for op, comp in zip(node.ops, node.comparators):
                fn = _ALLOWED_CMP.get(type(op))
                if fn is None:
                    raise UnsafeExpressionError(f"Disallowed operator: {type(op).__name__}")
                right = _eval(comp)
                if not fn(left, right):
                    return False
                left = right
            return True
        raise UnsafeExpressionError(f"Disallowed AST node: {type(node).__name__}")

    return bool(_eval(tree))
