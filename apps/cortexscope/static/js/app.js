import {
  classifyTokenHeat,
  computeBudgetSummary,
  computeWordDiff,
  escapeHtml,
  estimateTokensClient,
  formatBytes,
  formatMs,
} from "./metrics.js";
import {
  drawHardwareStrip,
  drawItlHistogram,
  drawScatterPlot,
  drawTokenTimeline,
} from "./charts.js";

const ARENA_PRESETS = {
  reasoning: "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than the ball. Explain carefully why naive intuition fails and state the exact price of the ball.",
  code: "Write a clean Python function `rapl_delta_uj(prev: int, curr: int, max_range: int) -> int` handling unsigned wraparound, plus 2 edge-case assertions.",
  linux: "On Intel Arrow Lake Linux running `xe` KMD, why can eDP PSR/Panel Replay trigger display wake hangs and how do kernel parameters mitigate it?",
  tr: "Yerel LLM çıkarımında TTFT ve ITL (Inter-Token Latency) gecikmelerinin kullanıcı deneyimine ve enerji tüketimine etkisini 4 maddede özetle.",
};

const state = {
  activeTab: "pulse",
  isLight: false,
  hwHistory: [],
  loadedModels: [],
  models: [],
  runs: [],
  currentHeatmapRun: null,
  selectedTokenSeq: null,
  liveRun: null,
  liveTokens: [],
  lastWorkbenchRunId: null,
  wsReconnectMs: 800,
};

const $ = (id) => document.getElementById(id);

function showToast(msg) {
  const el = $("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => el.classList.remove("show"), 2600);
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
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const active = b.dataset.tab === tab;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  document.querySelectorAll(".view").forEach((v) => {
    v.classList.toggle("active", v.id === `view-${tab}`);
  });
  if (tab === "pulse") renderHardwareChart();
  if (tab === "heatmap") renderHeatmapView();
  if (tab === "workbench" && $("wb-model").value) loadModelfile($("wb-model").value);
  if (tab === "history") loadHistoryAndStats();
}

function setArc(id, frac) {
  const el = $(id);
  if (!el) return;
  const c = 150.8;
  const clamped = Math.max(0.04, Math.min(1.0, frac));
  el.style.strokeDashoffset = String((c * (1 - clamped)).toFixed(1));
}

function renderTopKpis(hw, loadedModels) {
  if (!hw) return;
  $("kpi-pkg-w").textContent = hw.pkg_watts != null ? `${hw.pkg_watts.toFixed(1)} W` : "N/A";
  $("kpi-psys-w").textContent = hw.psys_watts != null ? `Psys: ${hw.psys_watts.toFixed(1)} W` : "Psys: N/A";
  $("kpi-temp").textContent = hw.temp_c != null ? `${hw.temp_c.toFixed(1)} °C` : "N/A";
  $("kpi-cpu").textContent = `CPU: ${hw.cpu_pct?.toFixed(0) ?? "—"}% / ${hw.cpu_freq_mhz ?? "—"} MHz`;
  $("kpi-gt0").textContent = hw.gt0_act_mhz != null ? `${hw.gt0_act_mhz} MHz` : "N/A";
  $("kpi-gt1").textContent = `GT1: ${hw.gt1_act_mhz ?? "—"} MHz // Throt: ${hw.gt0_throttle ?? "—"}`;
  $("kpi-ram").textContent = `${hw.ram_used_gb?.toFixed(1) ?? "—"} / ${hw.ram_total_gb?.toFixed(0) ?? "—"} GiB`;
  $("kpi-swap").textContent = `Swap: ${hw.swap_used_gb?.toFixed(2) ?? "0"} GiB`;

  setArc("arc-pkg-w", (hw.pkg_watts || 5) / 55);
  setArc("arc-temp", ((hw.temp_c || 40) - 25) / 75);
  setArc("arc-gt0", (hw.gt0_act_mhz || 150) / 2400);
  setArc("arc-ram", (hw.ram_used_gb || 8) / Math.max(1, hw.ram_total_gb || 32));

  const loaded = loadedModels || [];
  const totalVram = loaded.reduce((acc, m) => acc + (m.size_vram || 0), 0);
  $("kpi-vram").textContent = formatBytes(totalVram);
  $("kpi-loaded-count").textContent = `${loaded.length} model${loaded.length === 1 ? "" : "s"}`;
}

function renderLoadedModels(loadedModels) {
  const box = $("loaded-models-box");
  if (!loadedModels || loadedModels.length === 0) {
    box.innerHTML = `<div class="muted">No models currently resident in Ollama memory.</div>`;
    return;
  }
  box.innerHTML = loadedModels
    .map((m) => {
      const total = Math.max(1, m.size || 1);
      const vram = m.size_vram || 0;
      const pct = Math.min(100, Math.round((vram / total) * 100));
      const exp = m.expires_at ? escapeHtml(String(m.expires_at).slice(11, 19)) : "—";
      return `
        <div style="border:1px solid var(--border);padding:8px 10px;margin-bottom:8px;background:var(--bg-canvas);">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;">
            <strong class="mono">${escapeHtml(m.name)}</strong>
            <button class="btn btn-danger unload-btn" data-model="${escapeHtml(m.name)}" style="padding:2px 8px;font-size:11px;">Evict</button>
          </div>
          <div class="mono muted" style="font-size:11px;margin-top:3px;">
            VRAM ${formatBytes(vram)} / ${formatBytes(total)} (${pct}%) · ctx ${escapeHtml(m.context_length || "—")} · exp ${exp}
          </div>
          <div class="vram-bar"><div class="vram-fill" style="width:${pct}%"></div></div>
        </div>
      `;
    })
    .join("");
}

function renderHardwareChart() {
  drawHardwareStrip($("hw-strip-canvas"), state.hwHistory, state.isLight);
}

function renderPulseRecentRuns() {
  const body = $("pulse-recent-body");
  const list = state.runs.slice(0, 8);
  if (list.length === 0) {
    body.innerHTML = `<tr><td colspan="6" class="muted">No runs captured yet. Click "Run Probe" or send traffic to :11435.</td></tr>`;
    return;
  }
  body.innerHTML = list
    .map((r) => {
      const badgeCls = r.status === "completed" ? "badge-ok" : r.status === "aborted" ? "badge-warn" : "badge-err";
      return `
        <tr class="clickable open-run-row" data-run-id="${escapeHtml(r.id)}">
          <td class="mono">${escapeHtml(r.model)}</td>
          <td class="mono muted">${escapeHtml(r.source)}</td>
          <td class="num">${Number(r.tps || 0).toFixed(1)}</td>
          <td class="num">${formatMs(r.ttft_ms)}</td>
          <td class="num">${r.joules_per_100tok != null ? r.joules_per_100tok.toFixed(1) : "—"}</td>
          <td><span class="badge ${badgeCls}">${escapeHtml(r.status)}</span></td>
        </tr>
      `;
    })
    .join("");
}

function renderLiveStreamBox() {
  const box = $("live-stream-box");
  const meta = $("live-stream-meta");
  if (!state.liveRun) {
    meta.textContent = "Idle — route any Ollama client to :11435 or run Workbench";
    return;
  }
  const r = state.liveRun;
  meta.textContent = `${r.model} [${r.source}] · ${state.liveTokens.length} tok`;
  box.innerHTML = state.liveTokens
    .map((ev) => {
      const heat = classifyTokenHeat(ev.delta_ms, 25, 80);
      return `<span style="color:${heat.color}" title="#${ev.seq} ${ev.delta_ms}ms">${escapeHtml(ev.token_text)}</span>`;
    })
    .join("");
  box.scrollTop = box.scrollHeight;
}

function populateModelSelectors(models) {
  state.models = models || [];
  const names = state.models.map((m) => m.name);
  const fillSelect = (sel) => {
    const prev = sel.value;
    sel.innerHTML = names.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
    if (names.includes(prev)) sel.value = prev;
  };
  fillSelect($("quick-probe-model"));
  fillSelect($("wb-model"));

  const checks = $("arena-model-checks");
  const checkedPrev = new Set(Array.from(checks.querySelectorAll("input:checked")).map((i) => i.value));
  checks.innerHTML = names
    .map((n, idx) => {
      const isChecked = checkedPrev.size > 0 ? checkedPrev.has(n) : idx < 2;
      return `<label class="model-check"><input type="checkbox" value="${escapeHtml(n)}" ${isChecked ? "checked" : ""} /><span>${escapeHtml(n)}</span></label>`;
    })
    .join("");

  if (names.length > 0 && state.activeTab === "workbench") {
    loadModelfile($("wb-model").value);
  }
}

async function openHeatmapRun(runId) {
  if (!runId) return;
  try {
    const run = await apiJson(`/cx/runs/${encodeURIComponent(runId)}`);
    state.currentHeatmapRun = run;
    state.selectedTokenSeq = run.events?.[0]?.seq ?? null;
    switchTab("heatmap");
  } catch (err) {
    showToast(`Failed to load run: ${err.message}`);
  }
}

function renderHeatmapSelect() {
  const sel = $("hm-run-select");
  const prev = state.currentHeatmapRun?.id || sel.value;
  sel.innerHTML = state.runs
    .map((r) => `<option value="${escapeHtml(r.id)}">${escapeHtml(r.model)} · ${Number(r.tps || 0).toFixed(1)} tok/s · ${escapeHtml(r.prompt_preview.slice(0, 32))}</option>`)
    .join("");
  if (prev && state.runs.some((r) => r.id === prev)) sel.value = prev;
}

function renderHeatmapView() {
  renderHeatmapSelect();
  const run = state.currentHeatmapRun;
  if (!run) {
    if (state.runs.length > 0) {
      openHeatmapRun(state.runs[0].id);
    }
    return;
  }

  const phase = $("hm-phase-filter").value;
  const events = (run.events || []).filter((ev) => phase === "all" || ev.phase === phase);

  $("hm-summary-bar").innerHTML = `
    <span class="badge badge-cyan">${escapeHtml(run.model)}</span>
    <span>TPS: <strong>${Number(run.tps || 0).toFixed(1)}</strong></span>
    <span>TTFT: <strong>${formatMs(run.ttft_ms)}</strong></span>
    <span>P50 / P95 / P99: <strong>${run.itl_p50_ms} / ${run.itl_p95_ms} / ${run.itl_p99_ms} ms</strong></span>
    <span>Jitter σ: <strong>${run.itl_jitter_ms} ms</strong></span>
    <span>Think/Ans: <strong>${run.think_tokens}/${run.answer_tokens}</strong></span>
    <span>Energy: <strong>${run.pkg_joules != null ? `${run.pkg_joules.toFixed(2)} J (${run.joules_per_100tok ?? "—"} J/100t)` : "N/A"}</strong></span>
  `;

  // Top 5 slowest hesitation peaks
  const peaks = [...events].sort((a, b) => b.delta_ms - a.delta_ms).slice(0, 5);
  $("hm-peaks-bar").innerHTML =
    `<span class="kpi-label">Slowest Hesitation Peaks:</span>` +
    peaks
      .map(
        (pk) =>
          `<button class="btn peak-jump-btn" data-seq="${pk.seq}" style="padding:3px 8px;font-size:11px;">#${pk.seq} (${pk.delta_ms.toFixed(1)}ms) "${escapeHtml(pk.token_text.trim().slice(0, 12))}"</button>`
      )
      .join("");

  drawTokenTimeline(
    $("token-timeline-canvas"),
    events,
    state.selectedTokenSeq,
    run.itl_p50_ms || 25,
    run.itl_p95_ms || 80,
    state.isLight
  );

  const grid = $("hm-tokens");
  if (events.length === 0) {
    grid.innerHTML = `<span class="muted">No tokens in selected phase filter.</span>`;
  } else {
    grid.innerHTML = events
      .map((ev) => {
        const h = classifyTokenHeat(ev.delta_ms, run.itl_p50_ms || 25, run.itl_p95_ms || 80);
        const sel = ev.seq === state.selectedTokenSeq ? "selected" : "";
        const ph = ev.phase === "think" ? "phase-think" : "";
        const label = ev.token_text === "\n" ? "↵\n" : escapeHtml(ev.token_text);
        return `<span class="tok-chip ${sel} ${ph}" data-seq="${ev.seq}" style="color:${h.color};background:${h.bg};border-color:${h.border};" title="#${ev.seq} [${ev.phase}] ${ev.delta_ms}ms">${label}</span>`;
      })
      .join("");
  }

  renderTokenInspector(events.find((e) => e.seq === state.selectedTokenSeq) || events[0]);
}

function renderTokenInspector(ev) {
  const box = $("hm-token-inspector");
  if (!ev) {
    box.innerHTML = `<div class="muted">No token selected.</div>`;
    return;
  }
  box.innerHTML = `
    <div class="mono" style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
      <div><span class="kpi-label">Sequence</span><br/><strong>#${ev.seq}</strong></div>
      <div><span class="kpi-label">Phase</span><br/><span class="badge ${ev.phase === "think" ? "badge-warn" : "badge-cyan"}">${escapeHtml(ev.phase)}</span></div>
      <div><span class="kpi-label">ITL Delta</span><br/><strong>${ev.delta_ms.toFixed(2)} ms</strong></div>
      <div><span class="kpi-label">Cumulative</span><br/><strong>${formatMs(ev.elapsed_ms)}</strong></div>
      <div><span class="kpi-label">Pkg Power</span><br/><strong>${ev.pkg_watts != null ? `${ev.pkg_watts.toFixed(1)} W` : "N/A"}</strong></div>
      <div><span class="kpi-label">xe GT0 / Temp</span><br/><strong>${ev.gt_mhz ?? "—"} MHz / ${ev.temp_c ?? "—"} °C</strong></div>
    </div>
    <div class="kpi-label" style="margin-top:10px;">Raw Token String</div>
    <pre class="pre-code" style="margin-top:4px;">${escapeHtml(JSON.stringify(ev.token_text))}</pre>
  `;
}

async function runQuickProbe() {
  const model = $("quick-probe-model").value;
  if (!model) return showToast("No Ollama model available.");
  const btn = $("quick-probe-btn");
  btn.disabled = true;
  btn.textContent = "Probing...";
  try {
    const res = await apiJson("/cx/workbench/run", {
      method: "POST",
      body: JSON.stringify({
        model,
        prompt: "State in 2 terse bullet points why local RAPL energy telemetry matters for LLM inference.",
        think: false,
        temperature: 0.2,
        num_predict: 96,
      }),
    });
    showToast(`Probe finished: ${res.tps} tok/s, TTFT ${formatMs(res.ttft_ms)}`);
    await refreshRuns();
  } catch (err) {
    showToast(`Probe failed: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Run Probe";
  }
}

async function loadModelfile(modelName) {
  if (!modelName) return;
  try {
    const d = await apiJson(`/cx/models/show?name=${encodeURIComponent(modelName)}`);
    $("wb-caps").textContent = `capabilities: ${(d.capabilities || []).join(", ") || "completion"}`;
    const arch = d.model_info?.["general.architecture"] || d.details?.family || "gguf";
    $("wb-arch-badge").textContent = `${arch} · ${d.details?.quantization_level || ""}`;
    $("wb-params").textContent = d.parameters || "(defaults)";
    $("wb-modelfile").textContent = d.modelfile || "—";
    $("wb-template").textContent = d.template || "—";
    const ctxLen = Object.entries(d.model_info || {}).find(([k]) => k.endsWith(".context_length"))?.[1];
    if (ctxLen && Number(ctxLen) >= 512) {
      $("wb-ctx").max = String(Math.min(65536, Number(ctxLen)));
      $("wb-ctx").value = String(Math.min(8192, Number(ctxLen)));
      $("wb-ctx-val").textContent = $("wb-ctx").value;
    }
    updateWorkbenchBudget();
  } catch (err) {
    $("wb-modelfile").textContent = `Error: ${err.message}`;
  }
}

function updateWorkbenchBudget() {
  const ctx = Number($("wb-ctx").value) || 4096;
  const b = computeBudgetSummary($("wb-prompt").value, $("wb-system").value, ctx);
  $("wb-budget-label").textContent = `${b.tokens} / ${b.contextLength} tok (${b.utilPct}% · ${b.pressure.toUpperCase()})`;
  const bar = $("wb-budget-bar");
  bar.style.width = `${Math.min(100, b.utilPct)}%`;
  bar.style.background = b.pressure === "critical" ? "var(--crimson)" : b.pressure === "high" ? "var(--amber)" : "var(--cyan)";
}

async function executeWorkbench() {
  const model = $("wb-model").value;
  const prompt = $("wb-prompt").value.trim();
  if (!model || !prompt) return showToast("Select a model and enter a prompt.");
  const btn = $("wb-run-btn");
  btn.disabled = true;
  btn.textContent = "Running...";
  $("wb-output").textContent = "Streaming inference into CortexScope profiler...";
  try {
    const res = await apiJson("/cx/workbench/run", {
      method: "POST",
      body: JSON.stringify({
        model,
        prompt,
        system_prompt: $("wb-system").value,
        think: $("wb-think").checked,
        temperature: Number($("wb-temp").value),
        top_p: Number($("wb-topp").value),
        num_ctx: Number($("wb-ctx").value),
        num_predict: Number($("wb-pred").value),
      }),
    });
    state.lastWorkbenchRunId = res.id;
    $("wb-open-hm").disabled = false;
    $("wb-output").textContent =
      `[TPS ${res.tps} | TTFT ${formatMs(res.ttft_ms)} | P95 ${res.itl_p95_ms}ms | Energy ${res.pkg_joules ?? "N/A"}J]\n\n` +
      (res.thinking_text ? `<thinking>\n${res.thinking_text}\n</thinking>\n\n` : "") +
      (res.response_text || res.error_msg || "");
    await refreshRuns();
  } catch (err) {
    $("wb-output").textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Execute & Profile";
  }
}

async function launchArena() {
  const prompt = $("arena-prompt").value.trim();
  const models = Array.from(document.querySelectorAll("#arena-model-checks input:checked")).map((i) => i.value);
  if (!prompt) return showToast("Enter an Arena prompt.");
  if (models.length < 2 || models.length > 4) return showToast("Select 2 to 4 models for Arena.");

  const btn = $("arena-run-btn");
  btn.disabled = true;
  btn.textContent = "Running Match...";
  $("arena-status").textContent = `Benchmarking ${models.length} models sequentially...`;

  try {
    const match = await apiJson("/cx/arena/run", {
      method: "POST",
      body: JSON.stringify({
        prompt,
        models,
        think: $("arena-think").checked,
        temperature: Number($("arena-temp").value) || 0.2,
        num_predict: Number($("arena-maxtok").value) || 256,
      }),
    });
    renderArenaMatchResult(match);
    await loadArenaHistory();
    await refreshRuns();
    $("arena-status").textContent = `Completed match ${match.id}`;
  } catch (err) {
    showToast(`Arena error: ${err.message}`);
    $("arena-status").textContent = `Error: ${err.message}`;
  } finally {
    btn.disabled = false;
    btn.textContent = "Launch Arena Match";
  }
}

function renderArenaMatchResult(match) {
  const area = $("arena-results-area");
  const results = match.results || [];
  if (results.length === 0) return;

  const maxTps = Math.max(...results.map((r) => r.tps || 0));
  const minJ = Math.min(...results.filter((r) => r.joules_per_100tok != null).map((r) => r.joules_per_100tok));

  const cards = results
    .map((r) => {
      const fastBadge = r.tps === maxTps && maxTps > 0 ? `<span class="badge badge-ok">FASTEST</span>` : "";
      const effBadge = r.joules_per_100tok === minJ ? `<span class="badge badge-cyan">LOWEST J/100t</span>` : "";
      return `
        <div class="panel">
          <div class="panel-head">
            <span class="panel-title mono">${escapeHtml(r.model)}</span>
            <div style="display:flex;gap:4px;">${fastBadge}${effBadge}</div>
          </div>
          <div class="panel-body">
            <div class="mono" style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:10px;font-size:11px;">
              <div><span class="kpi-label">TPS</span><br/><strong>${Number(r.tps || 0).toFixed(1)}</strong></div>
              <div><span class="kpi-label">TTFT</span><br/><strong>${formatMs(r.ttft_ms)}</strong></div>
              <div><span class="kpi-label">P95 ITL</span><br/><strong>${r.itl_p95_ms}ms</strong></div>
              <div><span class="kpi-label">Tokens</span><br/><strong>${r.eval_tokens}</strong></div>
              <div><span class="kpi-label">J / 100t</span><br/><strong>${r.joules_per_100tok ?? "—"}</strong></div>
              <div><span class="kpi-label">Peak °C</span><br/><strong>${r.peak_temp_c ?? "—"}</strong></div>
            </div>
            <div class="stream-box" style="max-height:220px;">${escapeHtml(r.response_text || r.error_msg || "")}</div>
            <div style="display:flex;justify-content:space-between;margin-top:8px;">
              <button class="btn open-run-btn" data-run-id="${escapeHtml(r.run_id)}">Inspect Heatmap</button>
              <button class="btn btn-primary vote-btn" data-match-id="${escapeHtml(match.id)}" data-model="${escapeHtml(r.model)}">Vote Winner</button>
            </div>
          </div>
        </div>
      `;
    })
    .join("");

  let diffHtml = "";
  if (results.length >= 2) {
    const diff = computeWordDiff(results[0].response_text || "", results[1].response_text || "");
    diffHtml = `
      <div class="panel" style="margin-top:12px;">
        <div class="panel-head">
          <span class="panel-title">Word-Level Diff (${escapeHtml(results[0].model)} vs ${escapeHtml(results[1].model)})</span>
          <span class="mono badge badge-cyan">${diff.similarityPct}% Lexical Overlap</span>
        </div>
        <div class="panel-body grid-equal" style="margin-bottom:0;">
          <div class="stream-box">${diff.aHtml}</div>
          <div class="stream-box">${diff.bHtml}</div>
        </div>
      </div>
    `;
  }

  area.innerHTML = `<div class="arena-grid">${cards}</div>${diffHtml}`;
}

async function loadArenaHistory() {
  try {
    const data = await apiJson("/cx/arena/history");
    const body = $("arena-history-body");
    const matches = data.matches || [];
    if (matches.length === 0) {
      body.innerHTML = `<tr><td colspan="5" class="muted">No arena matches yet.</td></tr>`;
      return;
    }
    body.innerHTML = matches
      .map(
        (m) => `
        <tr class="clickable arena-load-row" data-match-id="${escapeHtml(m.id)}">
          <td class="mono">${escapeHtml(m.id)}</td>
          <td class="mono">${escapeHtml((m.models || []).join(" vs "))}</td>
          <td>${m.winner_model ? `<span class="badge badge-ok">${escapeHtml(m.winner_model)}</span>` : `<span class="muted">Unvoted</span>`}</td>
          <td>${escapeHtml((m.prompt_text || "").slice(0, 65))}</td>
          <td class="muted">${escapeHtml(m.notes || "")}</td>
        </tr>`
      )
      .join("");
    loadArenaHistory._cache = matches;
  } catch {}
}

async function refreshRuns() {
  try {
    const q = $("hist-search")?.value?.trim() || "";
    const src = $("hist-source")?.value || "";
    const qs = new URLSearchParams({ limit: "120" });
    if (q) qs.set("q", q);
    if (src) qs.set("source", src);
    const data = await apiJson(`/cx/runs?${qs.toString()}`);
    state.runs = data.runs || [];
    renderPulseRecentRuns();
    renderHistoryTable();
    if (state.activeTab === "heatmap") renderHeatmapSelect();
  } catch {}
}

function renderHistoryTable() {
  const body = $("history-body");
  if (!body) return;
  if (state.runs.length === 0) {
    body.innerHTML = `<tr><td colspan="12" class="muted">No matching runs found.</td></tr>`;
    return;
  }
  body.innerHTML = state.runs
    .map((r) => {
      const badgeCls = r.status === "completed" ? "badge-ok" : r.status === "aborted" ? "badge-warn" : "badge-err";
      return `
        <tr class="clickable open-run-row" data-run-id="${escapeHtml(r.id)}">
          <td class="mono muted">${escapeHtml(r.id)}</td>
          <td class="mono">${escapeHtml(r.model)}</td>
          <td class="mono">${escapeHtml(r.source)}</td>
          <td>${escapeHtml((r.prompt_preview || "").slice(0, 42))}</td>
          <td class="num">${r.think_tokens}/${r.answer_tokens}</td>
          <td class="num">${Number(r.tps || 0).toFixed(1)}</td>
          <td class="num">${formatMs(r.ttft_ms)}</td>
          <td class="num">${Number(r.itl_p95_ms || 0).toFixed(1)}ms</td>
          <td class="num">${r.joules_per_100tok != null ? r.joules_per_100tok.toFixed(1) : "—"}</td>
          <td class="num">${r.peak_temp_c != null ? r.peak_temp_c.toFixed(1) : "—"}</td>
          <td><span class="badge ${badgeCls}">${escapeHtml(r.status)}</span></td>
          <td><button class="btn btn-danger delete-run-btn" data-run-id="${escapeHtml(r.id)}" style="padding:2px 7px;font-size:11px;">Del</button></td>
        </tr>
      `;
    })
    .join("");
}

async function loadHistoryAndStats() {
  await refreshRuns();
  try {
    const stats = await apiJson("/cx/stats");
    const lb = $("leaderboard-body");
    const rows = stats.by_model || [];
    lb.innerHTML =
      rows.length === 0
        ? `<tr><td colspan="9" class="muted">No completed runs for leaderboard yet.</td></tr>`
        : rows
            .map(
              (m) => `
              <tr>
                <td class="mono"><strong>${escapeHtml(m.model)}</strong></td>
                <td class="num">${m.runs}</td>
                <td class="num">${m.total_tokens ?? 0}</td>
                <td class="num">${m.avg_tps ?? "—"}</td>
                <td class="num">${m.max_tps ?? "—"}</td>
                <td class="num">${formatMs(m.avg_ttft_ms)}</td>
                <td class="num">${m.avg_itl_p95_ms ?? "—"} ms</td>
                <td class="num">${m.avg_j_per_100tok ?? "—"}</td>
                <td class="num">${m.max_temp_c ?? "—"} °C</td>
              </tr>`
            )
            .join("");

    drawItlHistogram($("itl-hist-canvas"), stats.itl_histogram || [], state.isLight);
    drawScatterPlot($("scatter-canvas"), stats.scatter || [], state.isLight);
  } catch {}
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(`${proto}//${location.host}/ws/telemetry`);
  $("ws-label").textContent = "WS CONNECTING";

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
    if (msg.type === "hello") {
      if (msg.hw) {
        state.hwHistory.push(msg.hw);
        renderTopKpis(msg.hw, msg.loaded_models);
      }
      state.loadedModels = msg.loaded_models || [];
      renderLoadedModels(state.loadedModels);
      if (Array.isArray(msg.runs)) {
        state.runs = msg.runs;
        renderPulseRecentRuns();
      }
      renderHardwareChart();
    } else if (msg.type === "hw_tick") {
      state.hwHistory.push(msg.hw);
      if (state.hwHistory.length > 90) state.hwHistory.shift();
      state.loadedModels = msg.loaded_models || [];
      renderTopKpis(msg.hw, state.loadedModels);
      renderLoadedModels(state.loadedModels);
      if (state.activeTab === "pulse") renderHardwareChart();
    } else if (msg.type === "run_start") {
      state.liveRun = msg.run;
      state.liveTokens = [];
      $("kpi-live-tps").textContent = "STREAMING";
      $("kpi-live-ttft").textContent = `TTFT: waiting...`;
      renderLiveStreamBox();
    } else if (msg.type === "token_chunk") {
      const ev = msg.event;
      state.liveTokens.push(ev);
      if (state.liveTokens.length > 400) state.liveTokens.shift();
      if (ev.seq === 1) {
        $("kpi-live-ttft").textContent = `TTFT: ${formatMs(ev.delta_ms)}`;
      }
      if (ev.elapsed_ms > 0) {
        const approxTps = (ev.seq / (ev.elapsed_ms / 1000)).toFixed(1);
        $("kpi-live-tps").textContent = `${approxTps} tok/s`;
      }
      renderLiveStreamBox();
    } else if (msg.type === "run_complete") {
      const r = msg.run;
      $("kpi-live-tps").textContent = `${Number(r.tps || 0).toFixed(1)} tok/s`;
      $("kpi-live-ttft").textContent = `TTFT: ${formatMs(r.ttft_ms)}`;
      refreshRuns();
    } else if (msg.type === "arena_progress") {
      $("arena-status").textContent = `Arena [${msg.step}/${msg.total}] running ${msg.model}...`;
    }
  };

  ws.onclose = () => {
    $("ws-dot").className = "dot warn";
    $("ws-label").textContent = "WS RECONNECT";
    setTimeout(connectWs, state.wsReconnectMs);
    state.wsReconnectMs = Math.min(5000, state.wsReconnectMs * 1.5);
  };
}

async function bootstrap() {
  try {
    const [health, tel, modelsData] = await Promise.all([
      apiJson("/cx/health"),
      apiJson("/cx/telemetry/now?limit=90"),
      apiJson("/cx/models").catch(() => ({ models: [] })),
    ]);
    $("upstream-dot").className = health.upstream_ok ? "dot ok" : "dot err";
    $("upstream-label").textContent = health.upstream_ok ? "OLLAMA READY" : "OLLAMA DOWN";
    state.hwHistory = tel.history || [];
    state.loadedModels = tel.loaded_models || [];
    renderTopKpis(tel.hw, state.loadedModels);
    renderLoadedModels(state.loadedModels);
    renderHardwareChart();
    populateModelSelectors(modelsData.models || []);
  } catch (err) {
    showToast(`Init warning: ${err.message}`);
  }
  await refreshRuns();
  await loadArenaHistory();
  connectWs();
}

function wireEvents() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => switchTab(btn.dataset.tab));
  });

  window.addEventListener("keydown", (e) => {
    if (["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
    const map = { "1": "pulse", "2": "heatmap", "3": "arena", "4": "workbench", "5": "history" };
    if (map[e.key]) switchTab(map[e.key]);
    if (e.key.toLowerCase() === "r") refreshRuns();
  });

  window.addEventListener("resize", () => {
    if (state.activeTab === "pulse") renderHardwareChart();
    if (state.activeTab === "heatmap") renderHeatmapView();
    if (state.activeTab === "history") loadHistoryAndStats();
  });

  $("theme-btn").addEventListener("click", () => {
    state.isLight = !state.isLight;
    document.documentElement.setAttribute("data-theme", state.isLight ? "light" : "dark");
    renderHardwareChart();
    if (state.activeTab === "heatmap") renderHeatmapView();
    if (state.activeTab === "history") loadHistoryAndStats();
  });

  $("copy-proxy-btn").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText("OLLAMA_HOST=http://127.0.0.1:11435");
      showToast("Copied OLLAMA_HOST=http://127.0.0.1:11435");
    } catch {
      showToast("Use: OLLAMA_HOST=http://127.0.0.1:11435");
    }
  });

  $("refresh-ps-btn").addEventListener("click", async () => {
    const tel = await apiJson("/cx/telemetry/now?limit=90");
    renderTopKpis(tel.hw, tel.loaded_models);
    renderLoadedModels(tel.loaded_models);
  });

  $("quick-probe-btn").addEventListener("click", runQuickProbe);

  $("hm-run-select").addEventListener("change", (e) => openHeatmapRun(e.target.value));
  $("hm-phase-filter").addEventListener("change", renderHeatmapView);
  $("hm-export-csv").addEventListener("click", () => {
    if (state.currentHeatmapRun) window.open(`/cx/runs/${state.currentHeatmapRun.id}/export?format=csv`, "_blank");
  });
  $("hm-export-json").addEventListener("click", () => {
    if (state.currentHeatmapRun) window.open(`/cx/runs/${state.currentHeatmapRun.id}/export?format=json`, "_blank");
  });

  $("wb-model").addEventListener("change", (e) => loadModelfile(e.target.value));
  ["wb-temp", "wb-topp", "wb-ctx", "wb-pred"].forEach((id) => {
    $(id).addEventListener("input", () => {
      $(`${id}-val`).textContent = $(id).value;
      updateWorkbenchBudget();
    });
  });
  $("wb-prompt").addEventListener("input", updateWorkbenchBudget);
  $("wb-system").addEventListener("input", updateWorkbenchBudget);
  $("wb-run-btn").addEventListener("click", executeWorkbench);
  $("wb-open-hm").addEventListener("click", () => {
    if (state.lastWorkbenchRunId) openHeatmapRun(state.lastWorkbenchRunId);
  });

  document.querySelectorAll(".arena-preset").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("arena-prompt").value = ARENA_PRESETS[btn.dataset.preset] || "";
      $("arena-prompt-est").textContent = `${estimateTokensClient($("arena-prompt").value)} tok`;
    });
  });
  $("arena-prompt").addEventListener("input", () => {
    $("arena-prompt-est").textContent = `${estimateTokensClient($("arena-prompt").value)} tok`;
  });
  $("arena-run-btn").addEventListener("click", launchArena);

  $("hist-search").addEventListener("input", refreshRuns);
  $("hist-source").addEventListener("change", refreshRuns);
  $("hist-refresh-btn").addEventListener("click", loadHistoryAndStats);

  // Event delegation for dynamic cards, token chips, votes, deletes, and run opens
  document.body.addEventListener("click", async (e) => {
    const delBtn = e.target.closest(".delete-run-btn");
    if (delBtn) {
      e.stopPropagation();
      await apiJson(`/cx/runs/${encodeURIComponent(delBtn.dataset.runId)}`, { method: "DELETE" });
      showToast(`Deleted run ${delBtn.dataset.runId}`);
      loadHistoryAndStats();
      return;
    }

    const unloadBtn = e.target.closest(".unload-btn");
    if (unloadBtn) {
      await apiJson("/cx/models/unload", {
        method: "POST",
        body: JSON.stringify({ model: unloadBtn.dataset.model }),
      });
      showToast(`Evicted ${unloadBtn.dataset.model}`);
      const tel = await apiJson("/cx/telemetry/now?limit=90");
      renderLoadedModels(tel.loaded_models);
      return;
    }

    const chip = e.target.closest(".tok-chip, .peak-jump-btn");
    if (chip) {
      state.selectedTokenSeq = Number(chip.dataset.seq);
      renderHeatmapView();
      return;
    }

    const openBtn = e.target.closest(".open-run-row, .open-run-btn");
    if (openBtn) {
      openHeatmapRun(openBtn.dataset.runId);
      return;
    }

    const voteBtn = e.target.closest(".vote-btn");
    if (voteBtn) {
      await apiJson(`/cx/arena/${encodeURIComponent(voteBtn.dataset.matchId)}/vote`, {
        method: "POST",
        body: JSON.stringify({ winner_model: voteBtn.dataset.model, notes: "HUD user vote" }),
      });
      showToast(`Voted winner: ${voteBtn.dataset.model}`);
      loadArenaHistory();
      return;
    }

    const arenaRow = e.target.closest(".arena-load-row");
    if (arenaRow && loadArenaHistory._cache) {
      const m = loadArenaHistory._cache.find((x) => x.id === arenaRow.dataset.matchId);
      if (m) renderArenaMatchResult(m);
    }
  });
}

wireEvents();
bootstrap();
