import { buildFlamegraphSpans, statusStyle } from "./dag_utils.js";

function prepCanvas(canvas) {
  if (!canvas) return null;
  const host = canvas.parentElement || canvas;
  const rect = host.getBoundingClientRect();
  const w = Math.max(300, Math.floor(rect.width || host.clientWidth || 700));
  const h = Math.max(150, Math.floor(rect.height || host.clientHeight || 220));
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr;
    canvas.height = h * dpr;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  return { ctx, w, h };
}

export function drawFlamegraph(canvas, attempts, runStartedAt, selectedNodeId = null, isLight = false) {
  const prep = prepCanvas(canvas);
  if (!prep) return [];
  const { ctx, w, h } = prep;
  const textCol = isLight ? "#475569" : "#93A3BA";
  const gridCol = isLight ? "rgba(15,23,42,0.08)" : "rgba(148,163,184,0.12)";
  const pad = { l: 96, r: 22, t: 26, b: 22 };
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;

  if (!attempts || attempts.length === 0) {
    ctx.fillStyle = textCol;
    ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("Select a run to inspect worker-lane Gantt spans.", pad.l, pad.t + plotH / 2);
    return [];
  }

  const { spans, totalMs, lanes } = buildFlamegraphSpans(attempts, runStartedAt);
  const laneCount = Math.max(1, lanes.length);
  const laneH = Math.min(36, Math.max(20, plotH / laneCount));

  // Time grid ticks
  ctx.strokeStyle = gridCol;
  ctx.lineWidth = 1;
  ctx.fillStyle = textCol;
  ctx.font = "10px ui-monospace, monospace";
  for (let i = 0; i <= 5; i++) {
    const x = pad.l + (plotW * i) / 5;
    ctx.beginPath();
    ctx.moveTo(x, pad.t);
    ctx.lineTo(x, pad.t + laneCount * laneH);
    ctx.stroke();
    const labelMs = Math.round((totalMs * i) / 5);
    ctx.fillText(`${labelMs}ms`, Math.max(pad.l, x - 14), 15);
  }

  // Lane labels & alternating bands
  lanes.forEach((lane, idx) => {
    const y = pad.t + idx * laneH;
    if (idx % 2 === 0) {
      ctx.fillStyle = isLight ? "rgba(15,23,42,0.03)" : "rgba(255,255,255,0.02)";
      ctx.fillRect(pad.l, y, plotW, laneH);
    }
    ctx.fillStyle = textCol;
    ctx.font = "11px ui-monospace, monospace";
    ctx.fillText(lane, 8, y + laneH / 2 + 4);
  });

  const hitboxes = [];
  spans.forEach((sp) => {
    const y = pad.t + sp.laneIndex * laneH + 4;
    const barH = Math.max(10, laneH - 8);
    const qX = pad.l + (sp.queueOffsetMs / totalMs) * plotW;
    const sX = pad.l + (sp.startOffsetMs / totalMs) * plotW;
    const eX = pad.l + (sp.endOffsetMs / totalMs) * plotW;
    const barW = Math.max(4, eX - sX);

    // Queue wait sliver
    if (sX - qX > 1.5) {
      ctx.fillStyle = isLight ? "rgba(100,116,139,0.25)" : "rgba(148,163,184,0.25)";
      ctx.fillRect(qX, y + barH * 0.3, sX - qX, barH * 0.4);
    }

    const st = statusStyle(sp.phase === "compensate" ? "COMPENSATED" : sp.status);
    ctx.fillStyle = st.color;
    ctx.fillRect(sX, y, barW, barH);

    if (sp.node_id === selectedNodeId) {
      ctx.strokeStyle = "#FFFFFF";
      ctx.lineWidth = 1.6;
      ctx.strokeRect(sX - 1, y - 1, barW + 2, barH + 2);
    }

    ctx.font = "bold 10px ui-monospace, monospace";
    const fullLbl = `${sp.node_id}${sp.attempt > 1 ? `#${sp.attempt}` : ""} (${Math.round(sp.execMs)}ms)`;
    const shortLbl = `${sp.node_id} (${Math.round(sp.execMs)}ms)`;
    const txt = ctx.measureText(fullLbl).width <= barW - 8
      ? fullLbl
      : ctx.measureText(shortLbl).width <= barW - 8
        ? shortLbl
        : null;
    if (txt) {
      ctx.fillStyle = "#061018";
      ctx.fillText(txt, sX + 4, y + barH / 2 + 3);
    }

    hitboxes.push({ x: sX, y, w: barW, h: barH, span: sp });
  });

  return hitboxes;
}

export function drawThroughputChart(canvas, runs, isLight = false) {
  const prep = prepCanvas(canvas);
  if (!prep) return;
  const { ctx, w, h } = prep;
  const textCol = isLight ? "#475569" : "#93A3BA";
  const gridCol = isLight ? "rgba(15,23,42,0.08)" : "rgba(148,163,184,0.12)";
  const pad = { l: 46, r: 16, t: 22, b: 24 };
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;

  const recent = (runs || []).slice(0, 28).reverse();
  if (recent.length === 0) {
    ctx.fillStyle = textCol;
    ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("Execute runs or launch a burst benchmark to populate latency bars.", pad.l, pad.t + plotH / 2);
    return;
  }

  const maxDur = Math.max(50, ...recent.map((r) => Number(r.duration_ms) || 0));
  ctx.strokeStyle = gridCol;
  [0.25, 0.5, 0.75, 1].forEach((f) => {
    const y = pad.t + plotH * (1 - f);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  });

  const step = plotW / recent.length;
  const barW = Math.max(6, step * 0.65);

  recent.forEach((r, i) => {
    const dur = Number(r.duration_ms) || 10;
    const bh = Math.max(3, (dur / maxDur) * plotH);
    const x = pad.l + i * step + (step - barW) / 2;
    const y = pad.t + plotH - bh;
    const st = statusStyle(r.status);
    ctx.fillStyle = st.color;
    ctx.fillRect(x, y, barW, bh);
  });

  ctx.fillStyle = textCol;
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillText(`${Math.round(maxDur)}ms`, 4, pad.t + 8);
  ctx.fillText("0ms", 12, pad.t + plotH);
  ctx.fillText(`Last ${recent.length} runs (oldest → newest)`, pad.l, h - 6);
}
