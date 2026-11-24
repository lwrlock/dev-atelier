/**
 * Pure DAG geometry, state mapping, flamegraph spans, and run statistics helpers.
 */

const ESC_MAP = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(val) {
  if (val === null || val === undefined) return "";
  return String(val).replace(/[&<>"']/g, (c) => ESC_MAP[c]);
}

export function bezierEdgePath(x1, y1, x2, y2) {
  const dx = Math.max(45, Math.abs(x2 - x1) * 0.48);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

const STATE_STYLES = {
  PENDING: { color: "#94A3B8", bg: "rgba(148,163,184,0.12)", label: "PENDING" },
  QUEUED: { color: "#94A3B8", bg: "rgba(148,163,184,0.18)", label: "QUEUED" },
  LEASED: { color: "#38BDF8", bg: "rgba(56,189,248,0.16)", label: "LEASED" },
  RUNNING: { color: "#38BDF8", bg: "rgba(56,189,248,0.22)", label: "RUNNING" },
  RETRYING: { color: "#F59E0B", bg: "rgba(245,158,11,0.22)", label: "RETRYING" },
  SKIPPED: { color: "#94A3B8", bg: "rgba(71,85,105,0.24)", label: "SKIPPED" },
  FAILED: { color: "#EF4444", bg: "rgba(239,68,68,0.22)", label: "FAILED" },
  COMPENSATING: { color: "#F59E0B", bg: "rgba(245,158,11,0.24)", label: "ROLLBACK" },
  COMPENSATED: { color: "#FB923C", bg: "rgba(251,146,60,0.22)", label: "COMPENSATED" },
  COMPLETED: { color: "#10B981", bg: "rgba(16,185,129,0.20)", label: "COMPLETED" },
  DEAD_LETTERED: { color: "#EF4444", bg: "rgba(239,68,68,0.28)", label: "DLQ" },
};

export function statusStyle(status) {
  return STATE_STYLES[status] || { color: "#64748B", bg: "rgba(100,116,139,0.14)", label: String(status || "UNKNOWN") };
}

export function buildFlamegraphSpans(attempts = [], runStartedAt = 0) {
  if (!attempts || attempts.length === 0) {
    return { spans: [], totalMs: 100, lanes: [] };
  }
  const baseTs = runStartedAt || Math.min(...attempts.map((a) => a.queued_at || a.started_at || 0));
  const lanes = Array.from(new Set(attempts.map((a) => a.worker_id || "unassigned"))).sort();

  let maxEndMs = 50;
  const spans = attempts.map((a) => {
    const qAt = a.queued_at || a.started_at || baseTs;
    const sAt = a.started_at || qAt;
    const eAt = a.ended_at || (sAt + ((a.duration_ms || 10) / 1000));
    const queueOffsetMs = Math.max(0, (qAt - baseTs) * 1000);
    const startOffsetMs = Math.max(queueOffsetMs, (sAt - baseTs) * 1000);
    const endOffsetMs = Math.max(startOffsetMs + 2, (eAt - baseTs) * 1000);
    const queueWaitMs = Math.max(0, startOffsetMs - queueOffsetMs);
    const execMs = Math.max(2, a.duration_ms || (endOffsetMs - startOffsetMs));
    if (endOffsetMs > maxEndMs) maxEndMs = endOffsetMs;

    return {
      ...a,
      laneIndex: lanes.indexOf(a.worker_id || "unassigned"),
      queueOffsetMs,
      startOffsetMs,
      endOffsetMs,
      queueWaitMs,
      execMs,
    };
  });

  return { spans, totalMs: Math.max(50, maxEndMs), lanes };
}

export function computeRunStats(runs = []) {
  const total = runs.length;
  if (total === 0) {
    return { total: 0, completed: 0, compensated: 0, failed: 0, successRatePct: 0, p50Ms: 0, p95Ms: 0 };
  }
  let completed = 0;
  let compensated = 0;
  let failed = 0;
  const durs = [];
  for (const r of runs) {
    if (r.status === "COMPLETED") completed++;
    else if (r.status === "COMPENSATED") compensated++;
    else if (r.status === "FAILED") failed++;
    if (typeof r.duration_ms === "number" && Number.isFinite(r.duration_ms)) {
      durs.push(r.duration_ms);
    }
  }
  durs.sort((a, b) => a - b);
  const pct = (p) => {
    if (!durs.length) return 0;
    if (durs.length === 1) return Math.round(durs[0]);
    const idx = (p / 100) * (durs.length - 1);
    const lo = Math.floor(idx);
    const hi = Math.ceil(idx);
    return Math.round(durs[lo] + (durs[hi] - durs[lo]) * (idx - lo));
  };
  const p50Ms = pct(50);
  const p95Ms = pct(95);
  return {
    total,
    completed,
    compensated,
    failed,
    successRatePct: Math.round((completed / total) * 100),
    p50Ms,
    p95Ms,
  };
}
