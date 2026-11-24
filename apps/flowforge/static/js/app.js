import {
  bezierEdgePath,
  computeRunStats,
  escapeHtml,
  statusStyle,
} from "./dag_utils.js";
import { drawFlamegraph, drawThroughputChart } from "./canvas_viz.js";

const TAB_LABELS = {
  studio: "DAG Studio",
  timeline: "Time-Travel & Flamegraph",
  chaos: "Chaos & Saga Lab",
  fleet: "Worker Fleet & Burst",
  events: "Events, Cron & Export",
};

const RESOURCE_ACCENTS = {
  gateway: "#38BDF8",
  payments: "#10B981",
  inventory: "#0EA5E9",
  risk: "#F59E0B",
  gpu: "#22D3EE",
  k8s: "#38BDF8",
  storage: "#64748B",
  comms: "#94A3B8",
};

const state = {
  activeTab: "studio",
  inspTab: "cpm",
  runFilter: "all",
  isLight: false,
  workflows: [],
  selectedWfId: null,
  selectedWfData: null,
  selectedNodeId: null,
  showCpm: true,
  runs: [],
  activeRunId: null,
  liveNodeStates: {},
  selectedRunDetail: null,
  replayTimer: null,
  replaySpeedMs: 180,
  replaySeq: 0,
  workers: [],
  breakers: [],
  chaosRules: [],
  dlq: [],
  schedules: [],
  events: [],
  completedTasksTotal: 0,
  crashesRecoveredTotal: 0,
  pan: { x: 0, y: 0, scale: 1 },
  dragNode: null,
  panDrag: null,
  wsReconnectMs: 800,
};

const $ = (id) => document.getElementById(id);

function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.remove("show"), 2600);
}

async function apiJson(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
    ...opts,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const d = await res.json();
      detail = d.detail || d.error || detail;
    } catch {}
    throw new Error(detail);
  }
  return res.json();
}

function switchTab(tab) {
  state.activeTab = tab;
  document.querySelectorAll(".rail-btn").forEach((b) => {
    const act = b.dataset.tab === tab;
    b.classList.toggle("active", act);
    b.setAttribute("aria-selected", act ? "true" : "false");
  });
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${tab}`));
  $("crumb-view").textContent = TAB_LABELS[tab] || tab;
  if (tab === "timeline") loadTimelineView();
  if (tab === "chaos") loadChaosAndDlq();
  if (tab === "fleet") renderFleetView();
  if (tab === "events") loadEventsAndCron();
}

function switchInspectorTab(tab) {
  state.inspTab = tab;
  document.querySelectorAll("[data-insp]").forEach((b) => b.classList.toggle("active", b.dataset.insp === tab));
  ["cpm", "logs", "edit"].forEach((p) => {
    $(`insp-pane-${p}`).style.display = p === tab ? "block" : "none";
  });
}

function renderTopKpis() {
  const busy = state.workers.filter((w) => w.status === "BUSY").length;
  $("kpi-workers").textContent = `${busy} / ${state.workers.length}`;
  $("kpi-tasks").textContent = `${state.completedTasksTotal} tasks done`;
  $("kpi-crashes").textContent = `${state.crashesRecoveredTotal} crashed leases recovered`;

  const ledStrip = $("kpi-led-strip");
  ledStrip.innerHTML = state.workers
    .map((w) => `<span class="led ${w.status === "BUSY" ? "busy" : ""}" title="${escapeHtml(w.worker_id)}: ${escapeHtml(w.status)}"></span>`)
    .join("");

  const st = computeRunStats(state.runs);
  $("kpi-runs").textContent = `${st.total} runs`;
  $("nav-runs-count").textContent = String(st.total);
  $("kpi-sagas").textContent = `${st.compensated} Saga rollbacks triggered`;
  $("kpi-latency").textContent = st.total ? `${st.p50Ms} / ${st.p95Ms} ms` : "— ms";
  $("kpi-success-badge").textContent = st.total ? `${st.successRatePct}% OK` : "—%";
  $("kpi-success-bar").style.width = `${st.successRatePct}%`;

  const comp = state.selectedWfData?.compiled;
  $("kpi-makespan").textContent = comp ? `${comp.makespan_ms} ms` : "— ms";
  $("kpi-critpath").textContent = comp ? `${comp.critical_path.length} critical steps` : "0 steps";

  const unresolved = state.dlq.filter((d) => !d.resolved).length;
  $("kpi-dlq").textContent = String(unresolved);
  $("nav-dlq-count").textContent = String(unresolved);
  $("kpi-dlq-badge").className = unresolved > 0 ? "badge badge-err" : "badge badge-ok";
  $("kpi-dlq-badge").textContent = unresolved > 0 ? `${unresolved} OPEN` : "CLEAN";

  const chaosActive = state.chaosRules.some((r) => r.enabled);
  $("chaos-dot").className = chaosActive ? "dot err" : "dot ok";
  $("chaos-label").textContent = chaosActive ? `CHAOS (${state.chaosRules.length})` : "CHAOS OFF";
}

function renderWorkflowRail() {
  const box = $("wf-list");
  box.innerHTML = state.workflows
    .map((w) => {
      const act = w.id === state.selectedWfId ? "active" : "";
      const saga = w.saga_enabled ? `<span class="badge badge-cyan" style="padding:1px 5px;font-size:9px;">SAGA</span>` : "";
      return `
        <div class="wf-chip ${act}" data-wf-id="${escapeHtml(w.id)}">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:6px;">
            <strong style="font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">${escapeHtml(w.name)}</strong>
            ${saga}
          </div>
          <div class="mono muted" style="font-size:10px;margin-top:3px;">
            ${w.node_count} nodes · ${w.compiled?.makespan_ms ?? 0}ms CPM
          </div>
        </div>
      `;
    })
    .join("");

  const fill = (id) => {
    const sel = $(id);
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = state.workflows.map((w) => `<option value="${escapeHtml(w.id)}">${escapeHtml(w.name)}</option>`).join("");
    if (state.workflows.some((w) => w.id === prev)) sel.value = prev;
    else if (state.selectedWfId) sel.value = state.selectedWfId;
  };
  fill("burst-wf");
  fill("cron-wf");
}

function renderSvgTopology(svgEl, wfData, nodeStatesMap, options = {}) {
  if (!svgEl || !wfData) return;
  const { interactive = false, selectedNodeId = null, showCpm = true } = options;
  const wf = wfData.workflow;
  const comp = wfData.compiled;
  const critSet = new Set(comp.critical_path || []);
  const pos = comp.positions || {};

  const nodeW = 190;
  const nodeH = 68;

  const coords = (wf.nodes || []).map((n) => pos[n.id] || { x: 80, y: 100 });
  const minX = Math.min(...coords.map((p) => p.x), 40);
  const minY = Math.min(...coords.map((p) => p.y), 40);
  const maxX = Math.max(...coords.map((p) => p.x + nodeW), 620);
  const maxY = Math.max(...coords.map((p) => p.y + nodeH), 340);
  const padX = 44;
  const padY = 42;
  svgEl.setAttribute(
    "viewBox",
    `${minX - padX} ${minY - padY} ${Math.max(460, maxX - minX + padX * 2)} ${Math.max(280, maxY - minY + padY * 2)}`
  );

  const edgesSvg = (wf.edges || [])
    .map((e) => {
      const p1 = pos[e.source] || { x: 80, y: 100 };
      const p2 = pos[e.target] || { x: 320, y: 100 };
      const x1 = p1.x + nodeW;
      const y1 = p1.y + nodeH / 2;
      const x2 = p2.x;
      const y2 = p2.y + nodeH / 2;
      const isCrit = showCpm && critSet.has(e.source) && critSet.has(e.target);
      const srcState = nodeStatesMap[e.source];
      const dstState = nodeStatesMap[e.target];
      const isPulse = srcState === "COMPLETED" && (dstState === "RUNNING" || dstState === "QUEUED");

      const cls = `dag-edge ${isPulse ? "active-pulse" : isCrit ? "critical" : ""}`;
      const marker = isPulse ? "url(#arrow-active)" : isCrit ? "url(#arrow-crit)" : "url(#arrow-default)";
      const d = bezierEdgePath(x1, y1, x2, y2);
      const mx = (x1 + x2) / 2;
      const my = (y1 + y2) / 2;
      let condSvg = "";
      if (e.condition) {
        const txt = e.condition.replace("upstream.", "").slice(0, 18);
        const bw = Math.max(58, txt.length * 6.5 + 14);
        condSvg = `
          <rect x="${mx - bw / 2}" y="${my - 10}" width="${bw}" height="18" rx="9" fill="#0B1019" stroke="#314158" stroke-width="1"></rect>
          <text x="${mx}" y="${my + 2}" text-anchor="middle" fill="#CBD5E1" font-size="9.5" class="mono">${escapeHtml(txt)}</text>
        `;
      }
      return `<path class="${cls}" d="${d}" marker-end="${marker}"></path>${condSvg}`;
    })
    .join("");

  const nodesSvg = (wf.nodes || [])
    .map((n) => {
      const p = pos[n.id] || { x: 80, y: 100 };
      const stName = nodeStatesMap[n.id] || "PENDING";
      const st = statusStyle(stName);
      const cpm = comp.cpm?.[n.id];
      const isCrit = showCpm && cpm?.is_critical;
      const stroke = stName !== "PENDING" ? st.color : isCrit ? "#F59E0B" : "#314158";
      const selCls = n.id === selectedNodeId ? "selected" : "";
      const grpCol = RESOURCE_ACCENTS[n.resource_group] || "#38BDF8";
      const pillW = Math.max(70, st.label.length * 6.3 + 14);
      const shadowAttr = interactive ? `filter="url(#node-shadow)"` : "";
      const compBadge = n.compensate_handler
        ? `<text x="12" y="55" fill="#F59E0B" font-size="9.5" class="mono">↺ SAGA</text>`
        : `<text x="12" y="55" fill="#64748B" font-size="9.5" class="mono">${escapeHtml(n.resource_group.toUpperCase())}</text>`;

      return `
        <g class="dag-node-group" data-node-id="${escapeHtml(n.id)}" transform="translate(${p.x}, ${p.y})" ${shadowAttr}>
          <rect class="dag-node-rect ${selCls}" width="${nodeW}" height="${nodeH}" stroke="${stroke}" fill="#0F1522"></rect>
          <rect x="0" y="0" width="${nodeW}" height="3.5" rx="2" fill="${stroke}"></rect>
          <circle cx="15" cy="20" r="3.5" fill="${grpCol}"></circle>
          <text x="24" y="23" fill="#F1F5F9" font-size="12" font-weight="600">${escapeHtml(n.label.slice(0, 20))}</text>
          <text x="12" y="39" fill="#94A3B8" font-size="10" class="mono">${escapeHtml(n.id.slice(0, 14))} · ${n.estimated_ms}ms</text>
          ${compBadge}
          <rect x="${nodeW - pillW - 8}" y="44" width="${pillW}" height="17" rx="8.5" fill="${st.bg}" stroke="${st.color}" stroke-width="0.9"></rect>
          <text x="${nodeW - pillW / 2 - 8}" y="55.5" text-anchor="middle" fill="${st.color}" font-size="9" font-weight="700" class="mono">${escapeHtml(st.label)}</text>
        </g>
      `;
    })
    .join("");

  if (interactive) {
    const vp = svgEl.querySelector("#dag-viewport");
    vp.setAttribute("transform", `translate(${state.pan.x}, ${state.pan.y}) scale(${state.pan.scale})`);
    svgEl.querySelector("#dag-edges-layer").innerHTML = edgesSvg;
    svgEl.querySelector("#dag-nodes-layer").innerHTML = nodesSvg;
  } else {
    svgEl.innerHTML = `<g>${edgesSvg}${nodesSvg}</g>`;
  }
}

function renderStudioDag() {
  if (!state.selectedWfData) return;
  $("studio-wf-title").textContent = state.selectedWfData.workflow.name;
  $("crumb-wf").textContent = state.selectedWfData.workflow.id;
  renderSvgTopology($("dag-svg"), state.selectedWfData, state.liveNodeStates, {
    interactive: true,
    selectedNodeId: state.selectedNodeId,
    showCpm: state.showCpm,
  });
  renderNodeInspector();
}

function renderNodeInspector() {
  const wf = state.selectedWfData?.workflow;
  const comp = state.selectedWfData?.compiled;
  if (!wf || !state.selectedNodeId) {
    $("insp-pane-cpm").innerHTML = `<div class="muted">Select a node on the canvas to inspect CPM timing and spec.</div>`;
    $("insp-pane-logs").innerHTML = `<div class="muted">No node selected.</div>`;
    return;
  }
  const node = wf.nodes.find((n) => n.id === state.selectedNodeId);
  if (!node) return;
  const cpm = comp?.cpm?.[node.id] || {};
  const makespan = Math.max(1, comp?.makespan_ms || 100);
  const st = state.liveNodeStates[node.id] || "PENDING";
  const stInfo = statusStyle(st);
  const leftPct = Math.round(((cpm.es_ms || 0) / makespan) * 100);
  const widthPct = Math.max(6, Math.round((((cpm.ef_ms || 10) - (cpm.es_ms || 0)) / makespan) * 100));

  $("insp-pane-cpm").innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;">
      <div>
        <strong style="font-size:14px;">${escapeHtml(node.label)}</strong>
        <div class="mono muted" style="font-size:11px;">id: ${escapeHtml(node.id)}</div>
      </div>
      <span class="badge" style="color:${stInfo.color};border-color:${stInfo.color};background:${stInfo.bg}">${escapeHtml(st)}</span>
    </div>

    <div style="border:1px solid var(--border);border-radius:8px;padding:10px;background:var(--bg-canvas);margin-bottom:10px;">
      <div class="bento-head" style="margin-bottom:6px;">
        <span>Critical Path Window</span>
        <span class="badge ${cpm.is_critical ? "badge-warn" : "badge-ok"}">${cpm.is_critical ? "CRITICAL 0ms SLACK" : `${cpm.slack_ms}ms SLACK`}</span>
      </div>
      <div class="progress-track" style="height:8px;margin-bottom:6px;position:relative;">
        <div style="position:absolute;left:${leftPct}%;width:${widthPct}%;height:100%;border-radius:999px;background:${cpm.is_critical ? "var(--amber)" : "var(--cyan)"};"></div>
      </div>
      <div class="mono muted" style="display:flex;justify-content:space-between;font-size:10px;">
        <span>ES ${cpm.es_ms ?? 0}ms</span><span>EF ${cpm.ef_ms ?? 0}ms</span><span>Makespan ${makespan}ms</span>
      </div>
    </div>

    <div class="mono" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:11px;padding:10px;background:var(--bg-canvas);border:1px solid var(--border);border-radius:8px;">
      <div><span class="bento-sub">Executor</span><br/>${escapeHtml(node.handler)}</div>
      <div><span class="bento-sub">Saga Rollback</span><br/>${escapeHtml(node.compensate_handler || "None")}</div>
      <div><span class="bento-sub">Dependency Group</span><br/>${escapeHtml(node.resource_group)}</div>
      <div><span class="bento-sub">Trigger / Retries</span><br/>${escapeHtml(node.trigger_rule)} / ${node.max_retries}</div>
    </div>
  `;

  const attempt = state.selectedRunDetail?.attempts?.slice().reverse().find((a) => a.node_id === node.id);
  $("insp-pane-logs").innerHTML = attempt
    ? `
      <div class="mono muted" style="font-size:11px;margin-bottom:6px;">
        Attempt #${attempt.attempt} on <strong>${escapeHtml(attempt.worker_id || "—")}</strong> · ${attempt.duration_ms ?? 0}ms
      </div>
      <div class="bento-head" style="margin-bottom:4px;">Stdout / Error</div>
      <pre class="pre-code" style="margin-bottom:8px;">${escapeHtml(attempt.stdout_text || attempt.error_msg || "—")}</pre>
      <div class="bento-head" style="margin-bottom:4px;">JSON Output Payload</div>
      <pre class="pre-code">${escapeHtml(JSON.stringify(attempt.output || {}, null, 2))}</pre>
    `
    : `<div class="muted">Execute this workflow to capture task output &amp; stdout logs.</div>`;

  $("edit-node-fields").innerHTML = `
    <div style="display:flex;gap:6px;margin-bottom:8px;">
      <input id="edit-node-label" class="input" value="${escapeHtml(node.label)}" />
      <input id="edit-node-ms" type="number" class="input mono" value="${node.estimated_ms}" style="width:88px;" />
    </div>
    <button id="apply-node-edit-btn" class="btn" style="width:100%;justify-content:center;">Apply Edit to DAG</button>
  `;
}

function renderRecentRunsTable() {
  const body = $("studio-runs-body");
  const list = state.runs
    .filter((r) => state.runFilter === "all" || r.status === state.runFilter)
    .slice(0, 7);
  if (list.length === 0) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No matching executions found.</td></tr>`;
    return;
  }
  body.innerHTML = list
    .map((r) => {
      const st = statusStyle(r.status);
      return `
        <tr class="clickable open-tl-row" data-run-id="${escapeHtml(r.id)}">
          <td class="mono">${escapeHtml(r.id)}</td>
          <td>${escapeHtml(r.workflow_name || r.workflow_id)}</td>
          <td class="mono muted">${escapeHtml(r.trigger_type)}</td>
          <td><span class="badge" style="color:${st.color};border-color:${st.color};background:${st.bg}">${escapeHtml(r.status)}</span></td>
          <td class="mono">${r.saga_triggered ? '<span class="badge badge-warn">ROLLBACK</span>' : "—"}</td>
          <td class="num">${r.duration_ms != null ? `${r.duration_ms.toFixed(1)} ms` : "RUNNING"}</td>
        </tr>
      `;
    })
    .join("");
}

async function selectWorkflow(wfId) {
  state.selectedWfId = wfId;
  try {
    const data = await apiJson(`/api/workflows/${encodeURIComponent(wfId)}`);
    state.selectedWfData = data;
    state.selectedNodeId = data.workflow.nodes[0]?.id || null;
    state.liveNodeStates = {};
    renderWorkflowRail();
    renderTopKpis();
    renderStudioDag();
  } catch (err) {
    showToast(`Load workflow failed: ${err.message}`);
  }
}

async function executeRun(sagaDemo = false) {
  if (!state.selectedWfId) return;
  try {
    if (sagaDemo) {
      const nodes = state.selectedWfData.workflow.nodes;
      const target = nodes[Math.max(0, nodes.length - 2)]?.id || nodes[0].id;
      await apiJson("/api/chaos/rules", {
        method: "POST",
        body: JSON.stringify({ rules: [{ node_id: target, fail_prob: 1.0, latency_ms: 40, enabled: true }] }),
      });
      await refreshChaos();
    }
    state.liveNodeStates = {};
    const res = await apiJson(`/api/workflows/${encodeURIComponent(state.selectedWfId)}/run`, {
      method: "POST",
      body: JSON.stringify({ wait: true }),
    });
    state.activeRunId = res.run_id;
    $("studio-run-badge").textContent = `${res.run_id} · ${res.status} (${res.duration_ms}ms)`;
    if (sagaDemo) {
      await apiJson("/api/chaos/rules", { method: "POST", body: JSON.stringify({ rules: [] }) });
      await refreshChaos();
    }
    const detail = await apiJson(`/api/runs/${encodeURIComponent(res.run_id)}`);
    state.selectedRunDetail = detail;
    const snap = await apiJson(`/api/runs/${encodeURIComponent(res.run_id)}/replay`);
    state.liveNodeStates = snap.node_states || {};
    await refreshAllLists();
    renderStudioDag();
  } catch (err) {
    showToast(`Execution error: ${err.message}`);
  }
}

async function openTimelineRun(runId) {
  if (!runId) return;
  try {
    const detail = await apiJson(`/api/runs/${encodeURIComponent(runId)}`);
    state.selectedRunDetail = detail;
    state.activeRunId = runId;
    const maxSeq = detail.events?.length || 0;
    state.replaySeq = maxSeq;
    $("tl-slider").max = String(maxSeq);
    $("tl-slider").value = String(maxSeq);
    switchTab("timeline");
  } catch (err) {
    showToast(`Failed loading run: ${err.message}`);
  }
}

async function loadTimelineView() {
  const sel = $("tl-run-select");
  sel.innerHTML = state.runs
    .map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.id)} · ${escapeHtml(r.workflow_id)} · ${escapeHtml(r.status)}</option>`)
    .join("");
  if (state.activeRunId && state.runs.some((r) => r.id === state.activeRunId)) {
    sel.value = state.activeRunId;
  } else if (state.runs.length > 0 && !state.selectedRunDetail) {
    await openTimelineRun(state.runs[0].id);
    return;
  }
  await renderTimelineAtSeq(state.replaySeq);
}

async function renderTimelineAtSeq(seq) {
  const detail = state.selectedRunDetail;
  if (!detail) return;
  const events = detail.events || [];
  const maxSeq = events.length;
  state.replaySeq = Math.max(0, Math.min(maxSeq, Number(seq) || 0));
  $("tl-slider").value = String(state.replaySeq);

  $("tl-dots-rail").innerHTML = events
    .map((ev) => {
      const col = statusStyle(ev.state_to).color;
      const act = ev.seq === state.replaySeq ? "active" : "";
      return `<button class="seq-dot ${act}" data-seq="${ev.seq}" style="background:${col};" title="seq #${ev.seq}: ${escapeHtml(ev.event_type)} (${escapeHtml(ev.node_id)})"></button>`;
    })
    .join("");

  const snap = await apiJson(`/api/runs/${encodeURIComponent(detail.id)}/replay?seq=${state.replaySeq}`);
  $("tl-seq-label").textContent = `seq ${snap.seq} / ${maxSeq} (${snap.elapsed_ms.toFixed(1)}ms)`;
  const le = snap.last_event;
  $("tl-event-badge").textContent = le ? `${le.event_type} · ${le.node_id}` : "INITIAL";

  renderSvgTopology(
    $("tl-mini-svg"),
    { workflow: detail.workflow_spec, compiled: detail.compiled },
    snap.node_states || {},
    { interactive: false, showCpm: true }
  );

  const workerChips = Object.entries(snap.active_workers || {});
  const workersHtml = workerChips.length
    ? workerChips.map(([wid, nid]) => `<span class="badge badge-cyan">${escapeHtml(wid)} → ${escapeHtml(nid)}</span>`).join(" ")
    : `<span class="muted mono" style="font-size:11px;">All worker lanes idle at seq #${snap.seq}</span>`;

  $("tl-snapshot-box").innerHTML = `
    <div style="margin-bottom:10px;padding:9px 11px;border-radius:8px;background:var(--bg-canvas);border:1px solid var(--border);">
      <div class="bento-head" style="margin-bottom:4px;">State Transition</div>
      <div class="mono" style="font-size:12px;">
        ${le ? `<strong>${escapeHtml(le.node_id)}</strong>: ${escapeHtml(le.state_from)} → <span style="color:var(--cyan)">${escapeHtml(le.state_to)}</span>` : "Run initialized"}
      </div>
    </div>
    <div style="margin-bottom:10px;">
      <div class="bento-head" style="margin-bottom:6px;">Active Worker Occupancy</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;">${workersHtml}</div>
    </div>
    <div class="bento-head" style="margin-bottom:4px;">Event Detail</div>
    <pre class="pre-code">${escapeHtml(JSON.stringify(le?.detail || {}, null, 2))}</pre>
  `;

  drawFlamegraph(
    $("flamegraph-canvas"),
    detail.attempts || [],
    detail.started_at,
    le?.node_id || state.selectedNodeId,
    state.isLight
  );

  $("tl-attempts-body").innerHTML = (detail.attempts || [])
    .map((a) => {
      const st = statusStyle(a.status);
      return `
        <tr>
          <td class="mono">${escapeHtml(a.node_id)}</td>
          <td class="mono">#${a.attempt}</td>
          <td class="mono">${escapeHtml(a.phase)}</td>
          <td class="mono">${escapeHtml(a.worker_id || "—")}</td>
          <td><span class="badge" style="color:${st.color};border-color:${st.color};background:${st.bg}">${escapeHtml(a.status)}</span></td>
          <td class="num">${a.duration_ms != null ? `${a.duration_ms.toFixed(1)}ms` : "—"}</td>
          <td class="mono">${a.chaos_injected ? '<span class="badge badge-warn">YES</span>' : "—"}</td>
          <td class="mono muted">${escapeHtml((a.error_msg || a.stdout_text || "").slice(0, 68))}</td>
        </tr>
      `;
    })
    .join("");
}

async function refreshChaos() {
  const d = await apiJson("/api/chaos/rules");
  state.chaosRules = d.rules || [];
  $("chaos-rules-pre").textContent = JSON.stringify(state.chaosRules, null, 2);
  renderTopKpis();
}

async function loadChaosAndDlq() {
  const [cData, wData, dlqData] = await Promise.all([
    apiJson("/api/chaos/rules"),
    apiJson("/api/workers"),
    apiJson("/api/dlq"),
  ]);
  state.chaosRules = cData.rules || [];
  state.breakers = wData.breakers || [];
  state.dlq = dlqData.items || [];
  $("chaos-rules-pre").textContent = JSON.stringify(state.chaosRules, null, 2);

  $("breakers-box").innerHTML =
    state.breakers.length === 0
      ? `<div class="muted">No circuit breakers tripped yet.</div>`
      : state.breakers
          .map((b) => {
            const cls = b.state === "CLOSED" ? "badge-ok" : b.state === "HALF_OPEN" ? "badge-warn" : "badge-err";
            const healthPct = Math.max(0, Math.round(((b.failure_threshold - b.failure_count) / Math.max(1, b.failure_threshold)) * 100));
            return `
              <div style="border:1px solid var(--border);border-radius:8px;padding:9px 11px;margin-bottom:8px;background:var(--bg-canvas);">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;">
                  <strong class="mono">${escapeHtml(b.name)}</strong>
                  <span class="badge ${cls}">${escapeHtml(b.state)}</span>
                </div>
                <div class="progress-track" style="margin-bottom:4px;"><div class="progress-fill" style="width:${healthPct}%"></div></div>
                <div class="mono muted" style="font-size:10px;">Failures: ${b.failure_count}/${b.failure_threshold} · Successes: ${b.success_count}</div>
              </div>
            `;
          })
          .join("");

  $("dlq-body").innerHTML =
    state.dlq.length === 0
      ? `<tr><td colspan="8" class="muted">Dead-Letter Queue empty.</td></tr>`
      : state.dlq
          .map(
            (d) => `
          <tr>
            <td class="mono">#${d.id}</td>
            <td class="mono">${escapeHtml(d.run_id)}</td>
            <td class="mono">${escapeHtml(d.workflow_id)}</td>
            <td class="mono">${escapeHtml(d.node_id)}</td>
            <td class="mono">${d.attempt}</td>
            <td>${escapeHtml(d.reason.slice(0, 52))}</td>
            <td>${d.resolved ? '<span class="badge badge-ok">REPLAYED</span>' : '<span class="badge badge-err">OPEN</span>'}</td>
            <td><button class="btn replay-dlq-btn" data-dlq-id="${d.id}" ${d.resolved ? "disabled" : ""} style="padding:3px 9px;font-size:11px;">Replay</button></td>
          </tr>`
          )
          .join("");
  renderTopKpis();
}

function renderFleetView() {
  $("worker-grid").innerHTML = state.workers
    .map((w) => {
      const cls = w.status === "IDLE" ? "badge-ok" : w.status === "BUSY" ? "badge-cyan" : "badge-warn";
      const dotCls = w.status === "IDLE" ? "ok" : w.status === "BUSY" ? "warn" : "err";
      return `
        <div class="worker-card">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <span class="mono" style="font-weight:700;display:inline-flex;align-items:center;gap:6px;">
              <span class="dot ${dotCls}"></span>${escapeHtml(w.worker_id)}
            </span>
            <span class="badge ${cls}">${escapeHtml(w.status)}</span>
          </div>
          <div class="mono muted" style="font-size:11px;">
            Task: ${escapeHtml(w.current_node_id || "idle")}<br/>
            Completed: ${w.tasks_completed}<br/>
            Crashes Survived: ${w.crashes_survived}
          </div>
        </div>
      `;
    })
    .join("");
  drawThroughputChart($("throughput-canvas"), state.runs, state.isLight);
}

async function loadEventsAndCron() {
  const [evData, schData] = await Promise.all([apiJson("/api/events?limit=120"), apiJson("/api/schedules")]);
  state.events = evData.events || [];
  state.schedules = schData.schedules || [];

  if (state.selectedWfId) {
    const mmd = await fetch(`/api/workflows/${encodeURIComponent(state.selectedWfId)}/export?format=mermaid`).then((r) => r.text());
    $("mermaid-pre").textContent = mmd;
    $("webhook-pre").textContent =
      `curl -s -X POST ${location.origin}/api/workflows/${state.selectedWfId}/run \\\n` +
      `  -H "Content-Type: application/json" \\\n` +
      `  -d '{"idempotency_key":"order-42","params":{"amount":199}}'`;
  }

  $("cron-body").innerHTML =
    state.schedules.length === 0
      ? `<tr><td colspan="5" class="muted">No cron schedules configured.</td></tr>`
      : state.schedules
          .map(
            (s) => `
          <tr>
            <td class="mono">${escapeHtml(s.id)}</td>
            <td>${escapeHtml(s.workflow_name)}</td>
            <td class="mono">${escapeHtml(s.cron_expr)}</td>
            <td class="mono">${new Date(s.next_run_at * 1000).toISOString().slice(0, 19)}Z</td>
            <td><button class="btn btn-danger del-cron-btn" data-id="${escapeHtml(s.id)}" style="padding:2px 8px;font-size:11px;">Del</button></td>
          </tr>`
          )
          .join("");

  $("events-body").innerHTML = state.events
    .slice(0, 60)
    .map(
      (e) => `
      <tr class="clickable open-tl-row" data-run-id="${escapeHtml(e.run_id)}">
        <td class="mono">${escapeHtml(e.run_id)}</td>
        <td class="mono">#${e.seq}</td>
        <td class="mono">${escapeHtml(e.node_id)}</td>
        <td class="mono"><span class="badge badge-cyan">${escapeHtml(e.event_type)}</span></td>
        <td class="mono muted">${escapeHtml(e.state_from)} → ${escapeHtml(e.state_to)}</td>
        <td class="mono">${escapeHtml(e.worker_id || "—")}</td>
        <td class="mono muted">${escapeHtml(JSON.stringify(e.detail || {}).slice(0, 48))}</td>
      </tr>`
    )
    .join("");
}

function openCmdPalette() {
  $("cmd-backdrop").classList.add("open");
  $("cmd-input").value = "";
  renderCmdItems("");
  $("cmd-input").focus();
}

function renderCmdItems(query) {
  const q = query.trim().toLowerCase();
  const actions = [
    { label: "View: 1 DAG Studio", tag: "VIEW", fn: () => switchTab("studio") },
    { label: "View: 2 Time-Travel & Flamegraph", tag: "VIEW", fn: () => switchTab("timeline") },
    { label: "View: 3 Chaos & Saga Lab", tag: "VIEW", fn: () => switchTab("chaos") },
    { label: "View: 4 Worker Fleet & Burst", tag: "VIEW", fn: () => switchTab("fleet") },
    { label: "View: 5 Events & Cron", tag: "VIEW", fn: () => switchTab("events") },
    { label: "Action: Execute Active Workflow", tag: "RUN", fn: () => executeRun(false) },
    { label: "Action: Trigger Saga Demo Rollback", tag: "CHAOS", fn: () => executeRun(true) },
    { label: "Action: Toggle Light / Dark Theme", tag: "UI", fn: () => $("theme-btn").click() },
    ...state.workflows.map((w) => ({
      label: `Workflow: ${w.name}`,
      tag: "DAG",
      fn: () => {
        switchTab("studio");
        selectWorkflow(w.id);
      },
    })),
  ].filter((a) => !q || a.label.toLowerCase().includes(q));

  renderCmdItems._items = actions;
  $("cmd-list").innerHTML = actions
    .map(
      (a, idx) =>
        `<button class="cmd-item" data-cmd-idx="${idx}"><span>${escapeHtml(a.label)}</span><span class="badge badge-cyan">${escapeHtml(a.tag)}</span></button>`
    )
    .join("");
}

async function refreshAllLists() {
  const [wfRes, runRes, wRes, dlqRes] = await Promise.all([
    apiJson("/api/workflows"),
    apiJson("/api/runs?limit=60"),
    apiJson("/api/workers"),
    apiJson("/api/dlq"),
  ]);
  state.workflows = wfRes.workflows || [];
  state.runs = runRes.runs || [];
  state.workers = wRes.workers || [];
  state.breakers = wRes.breakers || [];
  state.completedTasksTotal = wRes.completed_tasks_total || 0;
  state.crashesRecoveredTotal = wRes.crashes_recovered_total || 0;
  state.dlq = dlqRes.items || [];

  renderWorkflowRail();
  renderRecentRunsTable();
  renderTopKpis();
  if (state.activeTab === "fleet") renderFleetView();
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/events`);
  ws.onopen = () => {
    state.wsReconnectMs = 800;
    $("ws-dot").className = "dot ok";
    $("ws-label").textContent = "WS LIVE";
  };
  ws.onmessage = (evt) => {
    let msg;
    try {
      msg = JSON.parse(evt.data);
    } catch {
      return;
    }
    if (msg.type === "hello" || msg.type === "workers_tick") {
      if (msg.workers) state.workers = msg.workers;
      if (msg.breakers) state.breakers = msg.breakers;
      renderTopKpis();
      if (state.activeTab === "fleet") renderFleetView();
    } else if (msg.type === "run_event") {
      const ev = msg.event;
      if (ev.run_id === state.activeRunId || !state.activeRunId) {
        state.activeRunId = ev.run_id;
        state.liveNodeStates[ev.node_id] = ev.state_to;
        if (state.activeTab === "studio") renderStudioDag();
      }
    } else if (msg.type === "run_finished") {
      refreshAllLists();
    }
  };
  ws.onclose = () => {
    $("ws-dot").className = "dot warn";
    $("ws-label").textContent = "WS RECONNECT";
    setTimeout(connectWs, state.wsReconnectMs);
    state.wsReconnectMs = Math.min(5000, state.wsReconnectMs * 1.5);
  };
}

function wireCanvasInteractions() {
  const svg = $("dag-svg");
  svg.addEventListener("mousedown", (e) => {
    const grp = e.target.closest(".dag-node-group");
    if (grp && state.selectedWfData) {
      const nid = grp.dataset.nodeId;
      state.selectedNodeId = nid;
      const p = state.selectedWfData.compiled.positions[nid] || { x: 80, y: 100 };
      state.dragNode = { nid, startClientX: e.clientX, startClientY: e.clientY, origX: p.x, origY: p.y };
      renderStudioDag();
      return;
    }
    state.panDrag = { startClientX: e.clientX, startClientY: e.clientY, origX: state.pan.x, origY: state.pan.y };
  });

  window.addEventListener("mousemove", (e) => {
    if (state.dragNode && state.selectedWfData) {
      const dx = (e.clientX - state.dragNode.startClientX) / state.pan.scale;
      const dy = (e.clientY - state.dragNode.startClientY) / state.pan.scale;
      const nx = Math.round(state.dragNode.origX + dx);
      const ny = Math.round(state.dragNode.origY + dy);
      state.selectedWfData.compiled.positions[state.dragNode.nid] = { x: nx, y: ny };
      const specNode = state.selectedWfData.workflow.nodes.find((n) => n.id === state.dragNode.nid);
      if (specNode) {
        specNode.x = nx;
        specNode.y = ny;
      }
      renderStudioDag();
    } else if (state.panDrag) {
      state.pan.x = state.panDrag.origX + (e.clientX - state.panDrag.startClientX);
      state.pan.y = state.panDrag.origY + (e.clientY - state.panDrag.startClientY);
      renderStudioDag();
    }
  });

  window.addEventListener("mouseup", () => {
    state.dragNode = null;
    state.panDrag = null;
  });

  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const factor = e.deltaY < 0 ? 1.08 : 0.92;
    state.pan.scale = Math.max(0.45, Math.min(2.2, Number((state.pan.scale * factor).toFixed(2))));
    renderStudioDag();
  });
}

function wireEvents() {
  document.querySelectorAll(".rail-btn").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  document.querySelectorAll("[data-insp]").forEach((b) => b.addEventListener("click", () => switchInspectorTab(b.dataset.insp)));
  document.querySelectorAll("[data-rfilter]").forEach((b) => {
    b.addEventListener("click", () => {
      state.runFilter = b.dataset.rfilter;
      document.querySelectorAll("[data-rfilter]").forEach((x) => x.classList.toggle("active", x === b));
      renderRecentRunsTable();
    });
  });

  $("chaos-pill").addEventListener("click", () => switchTab("chaos"));
  $("cmd-open-btn").addEventListener("click", openCmdPalette);
  $("cmd-backdrop").addEventListener("click", (e) => {
    if (e.target === $("cmd-backdrop")) $("cmd-backdrop").classList.remove("open");
  });
  $("cmd-input").addEventListener("input", (e) => renderCmdItems(e.target.value));

  window.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
      e.preventDefault();
      openCmdPalette();
      return;
    }
    if (e.key === "Escape") {
      $("cmd-backdrop").classList.remove("open");
      return;
    }
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const map = { "1": "studio", "2": "timeline", "3": "chaos", "4": "fleet", "5": "events" };
    if (map[e.key]) switchTab(map[e.key]);
  });

  $("theme-btn").addEventListener("click", () => {
    state.isLight = !state.isLight;
    document.documentElement.setAttribute("data-theme", state.isLight ? "light" : "dark");
    if (state.activeTab === "timeline") renderTimelineAtSeq(state.replaySeq);
    if (state.activeTab === "fleet") renderFleetView();
  });

  $("toggle-cpm-btn").addEventListener("click", () => {
    state.showCpm = !state.showCpm;
    $("toggle-cpm-btn").textContent = `CPM: ${state.showCpm ? "ON" : "OFF"}`;
    renderStudioDag();
  });

  $("zoom-in-btn").addEventListener("click", () => {
    state.pan.scale = Math.min(2.2, Number((state.pan.scale * 1.15).toFixed(2)));
    renderStudioDag();
  });
  $("zoom-out-btn").addEventListener("click", () => {
    state.pan.scale = Math.max(0.45, Number((state.pan.scale * 0.85).toFixed(2)));
    renderStudioDag();
  });

  $("fit-dag-btn").addEventListener("click", async () => {
    if (!state.selectedWfData) return;
    state.selectedWfData.workflow.nodes.forEach((n) => {
      n.x = null;
      n.y = null;
    });
    state.pan = { x: 0, y: 0, scale: 1 };
    const saved = await apiJson("/api/workflows", { method: "POST", body: JSON.stringify(state.selectedWfData.workflow) });
    state.selectedWfData = saved;
    renderStudioDag();
  });

  $("run-wf-btn").addEventListener("click", () => executeRun(false));
  $("run-saga-demo-btn").addEventListener("click", () => executeRun(true));

  $("save-wf-btn").addEventListener("click", async () => {
    if (!state.selectedWfData) return;
    try {
      const saved = await apiJson("/api/workflows", { method: "POST", body: JSON.stringify(state.selectedWfData.workflow) });
      state.selectedWfData = saved;
      await refreshAllLists();
      renderStudioDag();
      showToast("Workflow compiled & saved.");
    } catch (err) {
      showToast(`Compile error: ${err.message}`);
    }
  });

  $("add-node-btn").addEventListener("click", async () => {
    if (!state.selectedWfData) return;
    const nid = $("add-node-id").value.trim().replace(/\s+/g, "_");
    if (!nid) return showToast("Enter node_id");
    const parent = $("add-node-parent").value.trim();
    const est = Number($("add-node-ms").value) || 75;
    const wf = JSON.parse(JSON.stringify(state.selectedWfData.workflow));
    wf.nodes.push({ id: nid, label: nid, handler: "sim_compute", estimated_ms: est });
    if (parent) wf.edges.push({ source: parent, target: nid });
    try {
      const saved = await apiJson("/api/workflows", { method: "POST", body: JSON.stringify(wf) });
      state.selectedWfData = saved;
      state.selectedNodeId = nid;
      $("add-node-id").value = "";
      await refreshAllLists();
      renderStudioDag();
      showToast(`Added node ${nid}`);
    } catch (err) {
      showToast(`Invalid DAG edit: ${err.message}`);
    }
  });

  $("new-wf-btn").addEventListener("click", async () => {
    const id = `custom-${Date.now().toString(36).slice(-4)}`;
    const wf = {
      id,
      name: `Custom ${id}`,
      description: "User-designed workflow",
      saga_enabled: true,
      nodes: [
        { id: "extract", label: "Extract", handler: "sim_compute", estimated_ms: 50 },
        { id: "verify", label: "Verify", handler: "sim_compute", estimated_ms: 80 },
      ],
      edges: [{ source: "extract", target: "verify" }],
    };
    await apiJson("/api/workflows", { method: "POST", body: JSON.stringify(wf) });
    await refreshAllLists();
    await selectWorkflow(id);
  });

  $("tl-run-select").addEventListener("change", (e) => openTimelineRun(e.target.value));
  $("tl-slider").addEventListener("input", (e) => renderTimelineAtSeq(Number(e.target.value)));
  $("tl-start-btn").addEventListener("click", () => renderTimelineAtSeq(0));
  $("tl-prev-btn").addEventListener("click", () => renderTimelineAtSeq(state.replaySeq - 1));
  $("tl-next-btn").addEventListener("click", () => renderTimelineAtSeq(state.replaySeq + 1));
  $("tl-end-btn").addEventListener("click", () => renderTimelineAtSeq(Number($("tl-slider").max) || 0));
  $("tl-speed-btn").addEventListener("click", () => {
    state.replaySpeedMs = state.replaySpeedMs === 180 ? 85 : 180;
    $("tl-speed-btn").textContent = state.replaySpeedMs === 180 ? "1x" : "2x";
  });
  $("tl-play-btn").addEventListener("click", () => {
    if (state.replayTimer) {
      clearInterval(state.replayTimer);
      state.replayTimer = null;
      $("tl-play-btn").textContent = "▶ Play";
      return;
    }
    const maxSeq = Number($("tl-slider").max) || 0;
    if (state.replaySeq >= maxSeq) state.replaySeq = 0;
    $("tl-play-btn").textContent = "⏸ Pause";
    state.replayTimer = setInterval(() => {
      if (state.replaySeq >= maxSeq) {
        clearInterval(state.replayTimer);
        state.replayTimer = null;
        $("tl-play-btn").textContent = "▶ Play";
        return;
      }
      renderTimelineAtSeq(state.replaySeq + 1);
    }, state.replaySpeedMs);
  });

  $("chaos-add-btn").addEventListener("click", async () => {
    const rule = {
      node_id: $("chaos-node").value.trim() || "*",
      latency_ms: Number($("chaos-lat").value) || 0,
      fail_prob: Number($("chaos-prob").value) || 0,
      crash_worker_once: $("chaos-crash-once").checked,
      enabled: true,
    };
    await apiJson("/api/chaos/rules", { method: "POST", body: JSON.stringify({ rules: [...state.chaosRules, rule] }) });
    await loadChaosAndDlq();
    showToast("Chaos rule applied.");
  });

  $("chaos-clear-btn").addEventListener("click", async () => {
    await apiJson("/api/chaos/rules", { method: "POST", body: JSON.stringify({ rules: [] }) });
    await loadChaosAndDlq();
    showToast("Chaos cleared.");
  });

  document.querySelectorAll(".chaos-preset").forEach((b) => {
    b.addEventListener("click", async () => {
      const p = b.dataset.preset;
      const rules =
        p === "brownout"
          ? [{ node_id: "*", latency_ms: 350, fail_prob: 0.25, crash_worker_once: false, enabled: true }]
          : p === "crash"
            ? [{ node_id: "*", latency_ms: 40, fail_prob: 0.0, crash_worker_once: true, enabled: true }]
            : [{ node_id: "charge", latency_ms: 50, fail_prob: 1.0, crash_worker_once: false, enabled: true }];
      await apiJson("/api/chaos/rules", { method: "POST", body: JSON.stringify({ rules }) });
      await loadChaosAndDlq();
      showToast(`Chaos armed: ${p}`);
    });
  });

  $("reset-breakers-btn").addEventListener("click", async () => {
    await apiJson("/api/breakers/reset", { method: "POST" });
    await loadChaosAndDlq();
    showToast("Circuit breakers reset.");
  });
  $("refresh-dlq-btn").addEventListener("click", loadChaosAndDlq);

  $("burst-run-btn").addEventListener("click", async () => {
    const btn = $("burst-run-btn");
    btn.disabled = true;
    btn.textContent = "Bursting...";
    try {
      const res = await apiJson("/api/benchmark/burst", {
        method: "POST",
        body: JSON.stringify({
          workflow_id: $("burst-wf").value,
          count: Number($("burst-count").value) || 15,
        }),
      });
      $("burst-result-bar").textContent =
        `Burst: ${res.count} concurrent runs in ${res.wall_sec}s → ${res.runs_per_sec} runs/s | P50 ${res.p50_ms}ms | P95 ${res.p95_ms}ms | Outcomes: ${JSON.stringify(res.status_counts)}`;
      await refreshAllLists();
    } catch (err) {
      showToast(`Burst error: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Launch Burst";
    }
  });

  $("cron-add-btn").addEventListener("click", async () => {
    try {
      await apiJson("/api/schedules", {
        method: "POST",
        body: JSON.stringify({ workflow_id: $("cron-wf").value, cron_expr: $("cron-expr").value.trim() }),
      });
      await loadEventsAndCron();
      showToast("Cron scheduled.");
    } catch (err) {
      showToast(`Invalid cron: ${err.message}`);
    }
  });

  $("refresh-cron-btn").addEventListener("click", loadEventsAndCron);
  $("refresh-events-btn").addEventListener("click", loadEventsAndCron);
  $("copy-mermaid-btn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText($("mermaid-pre").textContent);
      showToast("Mermaid copied.");
    } catch {
      showToast("Mermaid ready.");
    }
  });

  document.body.addEventListener("click", async (e) => {
    const cmdBtn = e.target.closest(".cmd-item");
    if (cmdBtn && renderCmdItems._items) {
      const item = renderCmdItems._items[Number(cmdBtn.dataset.cmdIdx)];
      $("cmd-backdrop").classList.remove("open");
      if (item) item.fn();
      return;
    }

    const wfItem = e.target.closest(".wf-chip");
    if (wfItem) return selectWorkflow(wfItem.dataset.wfId);

    const openTl = e.target.closest(".open-tl-row");
    if (openTl) return openTimelineRun(openTl.dataset.runId);

    const seqDot = e.target.closest(".seq-dot");
    if (seqDot) return renderTimelineAtSeq(Number(seqDot.dataset.seq));

    const applyEdit = e.target.closest("#apply-node-edit-btn");
    if (applyEdit && state.selectedWfData && state.selectedNodeId) {
      const node = state.selectedWfData.workflow.nodes.find((n) => n.id === state.selectedNodeId);
      if (node) {
        node.label = $("edit-node-label").value.trim() || node.label;
        node.estimated_ms = Math.max(1, Number($("edit-node-ms").value) || node.estimated_ms);
        const saved = await apiJson("/api/workflows", { method: "POST", body: JSON.stringify(state.selectedWfData.workflow) });
        state.selectedWfData = saved;
        renderStudioDag();
        showToast("Node updated.");
      }
    }

    const replayBtn = e.target.closest(".replay-dlq-btn");
    if (replayBtn) {
      const res = await apiJson(`/api/dlq/${replayBtn.dataset.dlqId}/replay`, { method: "POST" });
      showToast(`DLQ Replayed → ${res.status}`);
      await loadChaosAndDlq();
      await refreshAllLists();
    }

    const delCron = e.target.closest(".del-cron-btn");
    if (delCron) {
      await apiJson(`/api/schedules/${encodeURIComponent(delCron.dataset.id)}`, { method: "DELETE" });
      await loadEventsAndCron();
    }
  });

  wireCanvasInteractions();
}

async function bootstrap() {
  wireEvents();
  await refreshChaos();
  await refreshAllLists();
  if (state.workflows.length > 0) {
    await selectWorkflow(state.workflows[0].id);
  }
  if (state.runs.length > 0) {
    state.activeRunId = state.runs[0].id;
    const detail = await apiJson(`/api/runs/${encodeURIComponent(state.activeRunId)}`);
    state.selectedRunDetail = detail;
    const snap = await apiJson(`/api/runs/${encodeURIComponent(state.activeRunId)}/replay`);
    state.liveNodeStates = snap.node_states || {};
    renderStudioDag();
  }
  connectWs();
}

bootstrap();
