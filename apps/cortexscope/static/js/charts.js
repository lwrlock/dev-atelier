import { classifyTokenHeat, computeSeriesExtent } from "./metrics.js";

function prepCanvas(canvas) {
  if (!canvas) return null;
  const host = canvas.parentElement || canvas;
  const rect = host.getBoundingClientRect();
  const w = Math.max(260, Math.floor(rect.width || host.clientWidth || 600));
  const h = Math.max(140, Math.floor(rect.height || host.clientHeight || 200));
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

function palette(isLight) {
  return {
    grid: isLight ? "rgba(28, 25, 23, 0.08)" : "rgba(234, 224, 200, 0.10)",
    axisText: isLight ? "#57534E" : "#ABA393",
    bgText: isLight ? "#78716C" : "#736B5C",
    cyan: isLight ? "#059669" : "#34D399",
    cyanFill: isLight ? "rgba(5, 150, 105, 0.14)" : "rgba(52, 211, 153, 0.15)",
    amber: isLight ? "#B45309" : "#EAB308",
    emerald: isLight ? "#047857" : "#10B981",
    crimson: isLight ? "#E11D48" : "#FB7185",
  };
}

export function drawHardwareStrip(canvas, history, isLight = false) {
  const prep = prepCanvas(canvas);
  if (!prep) return;
  const { ctx, w, h } = prep;
  const p = palette(isLight);
  const pad = { l: 42, r: 42, t: 16, b: 24 };
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;

  // Horizontal grid lines
  ctx.strokeStyle = p.grid;
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + (plotH * i) / 4;
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  }

  if (!history || history.length < 2) {
    ctx.fillStyle = p.bgText;
    ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("Collecting 1Hz hardware telemetry...", pad.l + 10, pad.t + plotH / 2);
    return;
  }

  const watts = history.map((d) => (typeof d.pkg_watts === "number" ? d.pkg_watts : d.cpu_pct * 0.45));
  const temps = history.map((d) => (typeof d.temp_c === "number" ? d.temp_c : 45));
  const cpus = history.map((d) => Number(d.cpu_pct) || 0);

  const wExt = computeSeriesExtent([...watts, 0, 45]);
  const tExt = computeSeriesExtent([...temps, 35, 90]);

  const xAt = (i) => pad.l + (i / (history.length - 1)) * plotW;
  const yNorm = (v, ext) => pad.t + plotH - ((v - ext.min) / (ext.max - ext.min)) * plotH;

  // CPU subtle dashed line (0..100%)
  ctx.save();
  ctx.strokeStyle = p.emerald;
  ctx.globalAlpha = 0.55;
  ctx.setLineDash([4, 4]);
  ctx.lineWidth = 1.4;
  ctx.beginPath();
  cpus.forEach((v, i) => {
    const x = xAt(i);
    const y = pad.t + plotH - (Math.min(100, Math.max(0, v)) / 100) * plotH;
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.restore();

  // Package Watts area + line
  ctx.beginPath();
  watts.forEach((v, i) => {
    const x = xAt(i);
    const y = yNorm(v, wExt);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = p.cyan;
  ctx.lineWidth = 2;
  ctx.stroke();

  ctx.lineTo(xAt(watts.length - 1), pad.t + plotH);
  ctx.lineTo(xAt(0), pad.t + plotH);
  ctx.closePath();
  ctx.fillStyle = p.cyanFill;
  ctx.fill();

  // Temp line
  ctx.beginPath();
  temps.forEach((v, i) => {
    const x = xAt(i);
    const y = yNorm(v, tExt);
    if (i === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.strokeStyle = p.amber;
  ctx.lineWidth = 1.8;
  ctx.stroke();

  // Axis labels
  ctx.fillStyle = p.cyan;
  ctx.font = "11px ui-monospace, monospace";
  ctx.fillText(`${Math.round(wExt.max)}W`, 4, pad.t + 10);
  ctx.fillText(`${Math.round(wExt.min)}W`, 4, pad.t + plotH);

  ctx.fillStyle = p.amber;
  ctx.fillText(`${Math.round(tExt.max)}°C`, w - pad.r + 6, pad.t + 10);
  ctx.fillText(`${Math.round(tExt.min)}°C`, w - pad.r + 6, pad.t + plotH);

  ctx.fillStyle = p.axisText;
  ctx.fillText(`-${history.length}s`, pad.l, h - 6);
  ctx.fillText("now", w - pad.r - 22, h - 6);
}

export function drawTokenTimeline(canvas, events, selectedSeq, p50 = 25, p95 = 80, isLight = false) {
  const prep = prepCanvas(canvas);
  if (!prep) return;
  const { ctx, w, h } = prep;
  const p = palette(isLight);
  const pad = { l: 44, r: 16, t: 14, b: 22 };
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;

  if (!events || events.length === 0) {
    ctx.fillStyle = p.bgText;
    ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("Select a run with captured token events.", pad.l, pad.t + plotH / 2);
    return;
  }

  const slice = events.slice(0, 260);
  const maxMs = Math.max(20, ...slice.map((e) => Number(e.delta_ms) || 0));

  // Grid
  ctx.strokeStyle = p.grid;
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75, 1].forEach((f) => {
    const y = pad.t + plotH * (1 - f);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  });

  const barStep = plotW / slice.length;
  const barW = Math.max(2, Math.min(12, barStep * 0.78));

  slice.forEach((ev, idx) => {
    const ms = Number(ev.delta_ms) || 0;
    const bh = Math.max(2, (ms / maxMs) * plotH);
    const x = pad.l + idx * barStep;
    const y = pad.t + plotH - bh;
    const heat = classifyTokenHeat(ms, p50, p95);
    ctx.fillStyle = ev.seq === selectedSeq ? "#FFFFFF" : heat.color;
    ctx.fillRect(x, y, barW, bh);
    if (ev.seq === selectedSeq) {
      ctx.strokeStyle = p.cyan;
      ctx.lineWidth = 1.5;
      ctx.strokeRect(x - 1, y - 1, barW + 2, bh + 2);
    }
  });

  // P95 threshold line
  if (p95 > 0 && p95 <= maxMs) {
    const y95 = pad.t + plotH - (p95 / maxMs) * plotH;
    ctx.save();
    ctx.strokeStyle = p.amber;
    ctx.setLineDash([4, 3]);
    ctx.beginPath();
    ctx.moveTo(pad.l, y95);
    ctx.lineTo(w - pad.r, y95);
    ctx.stroke();
    ctx.restore();
    ctx.fillStyle = p.amber;
    ctx.font = "10px ui-monospace, monospace";
    ctx.fillText(`P95 ${p95.toFixed(0)}ms`, w - pad.r - 68, Math.max(pad.t + 10, y95 - 3));
  }

  ctx.fillStyle = p.axisText;
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillText(`${Math.round(maxMs)}ms`, 4, pad.t + 10);
  ctx.fillText("0ms", 10, pad.t + plotH);
  ctx.fillText(`1..${slice.length} tok`, pad.l, h - 5);
}

export function drawItlHistogram(canvas, buckets, isLight = false) {
  const prep = prepCanvas(canvas);
  if (!prep) return;
  const { ctx, w, h } = prep;
  const p = palette(isLight);
  const pad = { l: 38, r: 16, t: 24, b: 26 };
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;

  if (!buckets || buckets.length === 0) return;
  const maxC = Math.max(1, ...buckets.map((b) => b.count || 0));
  const step = plotW / buckets.length;
  const barW = Math.max(18, step * 0.56);

  const colors = [p.cyan, "#38BDF8", p.emerald, p.amber, "#FB923C", p.crimson];
  buckets.forEach((b, i) => {
    const bh = (b.count / maxC) * plotH;
    const x = pad.l + i * step + (step - barW) / 2;
    const y = pad.t + plotH - bh;
    ctx.fillStyle = colors[i % colors.length];
    ctx.fillRect(x, y, barW, Math.max(2, bh));
    ctx.fillStyle = p.axisText;
    ctx.font = "11px ui-monospace, monospace";
    ctx.textAlign = "center";
    ctx.fillText(String(b.count), x + barW / 2, Math.max(13, y - 6));
    ctx.fillText(b.label, x + barW / 2, h - 7);
  });
  ctx.textAlign = "left";
}

export function drawScatterPlot(canvas, points, isLight = false) {
  const prep = prepCanvas(canvas);
  if (!prep) return;
  const { ctx, w, h } = prep;
  const p = palette(isLight);
  const pad = { l: 42, r: 16, t: 16, b: 26 };
  const plotW = w - pad.l - pad.r;
  const plotH = h - pad.t - pad.b;

  const valid = (points || []).filter((pt) => typeof pt.tps === "number" && pt.tps > 0);
  if (valid.length === 0) {
    ctx.fillStyle = p.bgText;
    ctx.font = "12px ui-monospace, monospace";
    ctx.fillText("Run inferences to populate TPS vs Thermal scatter.", pad.l, pad.t + plotH / 2);
    return;
  }

  const temps = valid.map((pt) => (typeof pt.peak_temp_c === "number" ? pt.peak_temp_c : 55));
  const tpsArr = valid.map((pt) => pt.tps);
  const xExt = computeSeriesExtent([...temps, 40, 85]);
  const yExt = computeSeriesExtent([...tpsArr, 0]);

  ctx.strokeStyle = p.grid;
  ctx.lineWidth = 1;
  [0.25, 0.5, 0.75, 1].forEach((f) => {
    const y = pad.t + plotH * (1 - f);
    ctx.beginPath();
    ctx.moveTo(pad.l, y);
    ctx.lineTo(w - pad.r, y);
    ctx.stroke();
  });

  const modelColors = [p.cyan, p.amber, p.emerald, "#F472B6", "#38BDF8"];
  const modelsSeen = [];

  valid.forEach((pt) => {
    if (!modelsSeen.includes(pt.model)) modelsSeen.push(pt.model);
    const col = modelColors[modelsSeen.indexOf(pt.model) % modelColors.length];
    const t = typeof pt.peak_temp_c === "number" ? pt.peak_temp_c : 55;
    const x = pad.l + ((t - xExt.min) / (xExt.max - xExt.min)) * plotW;
    const y = pad.t + plotH - ((pt.tps - yExt.min) / (yExt.max - yExt.min)) * plotH;
    ctx.beginPath();
    ctx.arc(x, y, 4.5, 0, Math.PI * 2);
    ctx.fillStyle = col;
    ctx.fill();
  });

  ctx.fillStyle = p.axisText;
  ctx.font = "10px ui-monospace, monospace";
  ctx.fillText(`${yExt.max.toFixed(0)} tok/s`, 4, pad.t + 10);
  ctx.fillText(`${yExt.min.toFixed(0)}`, 4, pad.t + plotH);
  ctx.fillText(`${Math.round(xExt.min)}°C`, pad.l, h - 6);
  ctx.fillText(`${Math.round(xExt.max)}°C`, w - pad.r - 28, h - 6);
}
