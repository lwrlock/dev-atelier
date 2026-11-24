import test from "node:test";
import assert from "node:assert/strict";

import {
  escapeHtml,
  bezierEdgePath,
  statusStyle,
  buildFlamegraphSpans,
  computeRunStats,
} from "../static/js/dag_utils.js";

test("escapeHtml escapes dangerous HTML payloads", () => {
  const dirty = `<svg onload="alert(1)">&'test'`;
  const clean = escapeHtml(dirty);
  assert.equal(clean.includes("<"), false);
  assert.equal(clean.includes(">"), false);
  assert.equal(clean, "&lt;svg onload=&quot;alert(1)&quot;&gt;&amp;&#39;test&#39;");
});

test("bezierEdgePath produces valid cubic SVG path", () => {
  const d = bezierEdgePath(100, 120, 350, 200);
  assert.ok(d.startsWith("M 100 120 C "));
  assert.ok(d.endsWith(" 350 200"));
});

test("statusStyle covers all FlowForge task states cleanly", () => {
  const states = [
    "PENDING", "QUEUED", "LEASED", "RUNNING", "RETRYING",
    "SKIPPED", "FAILED", "COMPENSATING", "COMPENSATED", "COMPLETED", "DEAD_LETTERED",
  ];
  for (const s of states) {
    const st = statusStyle(s);
    assert.ok(st.color.startsWith("#"));
    assert.ok(st.label.length > 0);
  }
});

test("buildFlamegraphSpans computes queue wait and worker lane offsets", () => {
  const base = 1000.0;
  const attempts = [
    { node_id: "a", attempt: 1, phase: "exec", worker_id: "worker-02", status: "COMPLETED", queued_at: base, started_at: base + 0.02, ended_at: base + 0.12, duration_ms: 100 },
    { node_id: "b", attempt: 1, phase: "compensate", worker_id: "worker-01", status: "COMPENSATED", queued_at: base + 0.15, started_at: base + 0.16, ended_at: base + 0.21, duration_ms: 50 },
  ];
  const { spans, totalMs, lanes } = buildFlamegraphSpans(attempts, base);
  assert.equal(spans.length, 2);
  assert.equal(lanes.includes("worker-01") && lanes.includes("worker-02"), true);
  assert.ok(spans[0].queueWaitMs >= 19 && spans[0].queueWaitMs <= 21);
  assert.ok(totalMs >= 210);
});

test("computeRunStats aggregates success rate and latency percentiles", () => {
  const runs = [
    { status: "COMPLETED", duration_ms: 100 },
    { status: "COMPLETED", duration_ms: 200 },
    { status: "COMPENSATED", duration_ms: 350 },
    { status: "COMPLETED", duration_ms: 150 },
  ];
  const st = computeRunStats(runs);
  assert.equal(st.total, 4);
  assert.equal(st.completed, 3);
  assert.equal(st.compensated, 1);
  assert.equal(st.p50Ms, 175);
  assert.ok(st.p95Ms >= 320);
});
