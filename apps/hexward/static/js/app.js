import {
  entropyZoneColor,
  escapeHtml,
  formatBytes,
  moveArrayItem,
  normalizeAobInput,
} from "./hexward_utils.js";

const state = {
  scan: null,
  sevFilter: "ALL",
  resolutions: {},
  currentBinary: null,
};

async function apiJson(url, opts = {}) {
  const res = await fetch(url, opts);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

function setStage(stageName) {
  document.querySelectorAll(".stage-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.stage === stageName);
  });
  document.querySelectorAll(".stage-view").forEach((sec) => {
    sec.classList.toggle("active", sec.id === `stage-${stageName}`);
  });
}

function renderVfsStage() {
  const { scan, sevFilter } = state;
  if (!scan) return;

  document.getElementById("kpi-mods").textContent = String(scan.summary.mod_count);
  document.getElementById("kpi-collisions").textContent = String(scan.summary.total_collisions);
  document.getElementById("kpi-critical").textContent = String(scan.summary.critical_collisions);
  document.getElementById("kpi-topo").textContent = scan.summary.has_cycle ? "CYCLE!" : "CLEAN";

  const stackEl = document.getElementById("mod-stack-list");
  stackEl.innerHTML = scan.mods
    .map((m, idx) => {
      const deps = m.requires.length ? m.requires.map(escapeHtml).join(", ") : "None";
      return `
        <div class="mod-card">
          <div class="mod-card-top">
            <div style="display:flex;align-items:center;gap:8px;">
              <span class="mod-priority">P${idx + 1}</span>
              <span class="mod-name">${escapeHtml(m.name)}</span>
            </div>
            <div style="display:flex;gap:4px;">
              <button class="btn" data-move="${idx}:-1" ${idx === 0 ? "disabled" : ""} type="button">▲</button>
              <button class="btn" data-move="${idx}:1" ${idx === scan.mods.length - 1 ? "disabled" : ""} type="button">▼</button>
            </div>
          </div>
          <div class="mod-meta">
            <span>v${escapeHtml(m.version)} · ${escapeHtml(m.author)}</span>
            <span>Wins ${m.winning_files}/${m.file_count} (${m.dominance_pct}%)</span>
            <span>Deps: ${deps}</span>
          </div>
          <div class="dom-bar-track"><div class="dom-bar-fill" style="width:${m.dominance_pct}%"></div></div>
        </div>
      `;
    })
    .join("");

  stackEl.querySelectorAll("[data-move]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [idxStr, dirStr] = btn.dataset.move.split(":");
      const idx = Number(idxStr);
      const next = moveArrayItem(
        state.scan.mods.map((m) => m.id),
        idx,
        idx + Number(dirStr)
      );
      state.scan = await apiJson("/api/mods/reorder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ordered_ids: next }),
      });
      renderVfsStage();
    });
  });

  const rows = scan.collisions.filter((c) => sevFilter === "ALL" || c.severity === sevFilter);
  const tbody = document.getElementById("collision-tbody");
  tbody.innerHTML = rows
    .map((c) => {
      const isMergeable = c.rel_path.endsWith(".ini") || c.rel_path.endsWith(".json");
      const actionBtn = isMergeable
        ? `<button class="btn btn-rose" data-open-merge="${escapeHtml(c.rel_path)}|${escapeHtml(c.losers[0])}|${escapeHtml(c.winner)}" type="button">3-Way Resolve →</button>`
        : `<span style="color:var(--text-muted);font-size:11px;">Last-Win Script</span>`;
      return `
        <tr>
          <td><span class="sev-badge sev-${escapeHtml(c.severity)}">${escapeHtml(c.severity)}</span></td>
          <td>${escapeHtml(c.rel_path)}</td>
          <td style="color:var(--neon-mint)">${escapeHtml(c.winner)}</td>
          <td style="color:var(--text-muted)">${c.losers.map(escapeHtml).join(", ")}</td>
          <td>${actionBtn}</td>
        </tr>
      `;
    })
    .join("");

  tbody.querySelectorAll("[data-open-merge]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [rel, modA, modB] = btn.dataset.openMerge.split("|");
      document.getElementById("merge-file-select").value = rel;
      document.getElementById("merge-moda-select").value = modA;
      document.getElementById("merge-modb-select").value = modB;
      state.resolutions = {};
      setStage("merge");
      await runThreeWayMerge();
    });
  });
}

function populateSelectors() {
  const mods = state.scan.mods;
  const modA = document.getElementById("merge-moda-select");
  const modB = document.getElementById("merge-modb-select");
  const opts = mods.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join("");
  modA.innerHTML = opts;
  modB.innerHTML = opts;
  if (mods.length >= 2) {
    modA.value = mods[0].id;
    modB.value = mods[1].id;
  }

  const binSel = document.getElementById("binary-select");
  binSel.innerHTML = state.scan.binaries
    .map((b) => `<option value="${escapeHtml(b.name)}">${escapeHtml(b.name)} (${formatBytes(b.size_bytes)})</option>`)
    .join("");

  const texSel = document.getElementById("texture-select");
  texSel.innerHTML = state.scan.textures
    .map((t) => `<option value="${escapeHtml(t.name)}">${escapeHtml(t.name)}</option>`)
    .join("");
}

async function runThreeWayMerge() {
  const relPath = document.getElementById("merge-file-select").value;
  const modA = document.getElementById("merge-moda-select").value;
  const modB = document.getElementById("merge-modb-select").value;
  const data = await apiJson("/api/merge/three-way", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      rel_path: relPath,
      mod_a: modA,
      mod_b: modB,
      resolutions: state.resolutions,
    }),
  });

  document.getElementById("merge-summary-bar").innerHTML = `
    <span>Format: <b>${escapeHtml(data.format.toUpperCase())}</b></span>
    <span style="color:var(--neon-mint)">Auto-Merged Keys: <b>${data.auto_merged_count}</b></span>
    <span style="color:var(--neon-rose)">Conflicting Keys: <b>${data.conflicts.length}</b></span>
  `;
  document.getElementById("hdr-ours").textContent = `MOD A (${modA})`;
  document.getElementById("hdr-theirs").textContent = `MOD B (${modB})`;
  document.getElementById("pre-base").textContent = data.base_text || "(none)";
  document.getElementById("pre-ours").textContent = data.ours_text || "(unchanged)";
  document.getElementById("pre-theirs").textContent = data.theirs_text || "(unchanged)";
  document.getElementById("pre-merged").textContent = data.merged_text || "";

  const box = document.getElementById("merge-conflicts-box");
  box.innerHTML = data.conflicts
    .map(
      (c) => `
      <div class="conflict-card">
        <div style="display:flex;justify-content:space-between;align-items:center;">
          <strong style="font-family:var(--font-mono);font-size:12px;">${escapeHtml(c.key)}</strong>
          <span class="sev-badge sev-CONFIG">${escapeHtml(c.selected)}</span>
        </div>
        <div class="mod-meta">
          <span>Base: ${escapeHtml(String(c.base))}</span>
          <span>Ours: ${escapeHtml(String(c.ours))}</span>
          <span>Theirs: ${escapeHtml(String(c.theirs))}</span>
        </div>
        <div class="conflict-choices">
          <button class="btn ${c.selected === "keep_ours" ? "btn-rose" : ""}" data-resolve="${escapeHtml(c.key)}|keep_ours" type="button">Keep Ours</button>
          <button class="btn ${c.selected === "take_theirs" ? "btn-rose" : ""}" data-resolve="${escapeHtml(c.key)}|take_theirs" type="button">Take Theirs</button>
          <button class="btn ${c.selected === "keep_base" ? "btn-rose" : ""}" data-resolve="${escapeHtml(c.key)}|keep_base" type="button">Keep Base</button>
        </div>
      </div>`
    )
    .join("");

  box.querySelectorAll("[data-resolve]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const [key, choice] = btn.dataset.resolve.split("|");
      state.resolutions[key] = choice;
      await runThreeWayMerge();
    });
  });
}

function drawEntropyCanvas(profile) {
  const canvas = document.getElementById("entropy-canvas");
  const ctx = canvas.getContext("2d");
  const w = canvas.width;
  const h = canvas.height;
  ctx.clearRect(0, 0, w, h);

  ctx.fillStyle = "#0b0618";
  ctx.fillRect(0, 0, w, h);

  // Packed threshold 6.8/8.0
  const threshY = h - (6.8 / 8.0) * (h - 20) - 10;
  ctx.strokeStyle = "rgba(244, 63, 94, 0.55)";
  ctx.setLineDash([5, 4]);
  ctx.beginPath();
  ctx.moveTo(0, threshY);
  ctx.lineTo(w, threshY);
  ctx.stroke();
  ctx.setLineDash([]);

  if (!profile.length) return;
  const barW = Math.max(4, (w - 20) / profile.length);
  profile.forEach((p, idx) => {
    const bh = (p.entropy / 8.0) * (h - 24);
    const x = 10 + idx * barW;
    const y = h - 10 - bh;
    ctx.fillStyle = entropyZoneColor(p.entropy);
    ctx.fillRect(x, y, Math.max(2, barW - 2), bh);
  });

  ctx.fillStyle = "#fda4af";
  ctx.font = "11px monospace";
  ctx.fillText("CRYPT THRESHOLD (6.8 bits/B)", 12, Math.max(14, threshY - 5));
}

function renderHexRows(hexData) {
  document.getElementById("hex-range-label").textContent = `0x${hexData.start.toString(16).padStart(6, "0")} .. 0x${hexData.end.toString(16).padStart(6, "0")}`;
  document.getElementById("hex-rows-box").innerHTML = hexData.rows
    .map(
      (r) => `<div class="hex-row"><span class="hex-off">${escapeHtml(r.offset)}</span><span class="hex-bytes">${escapeHtml(r.hex)}</span><span class="hex-ascii">${escapeHtml(r.ascii)}</span></div>`
    )
    .join("");
}

async function inspectBinary(name) {
  const data = await apiJson(`/api/binary/dissect?name=${encodeURIComponent(name)}`);
  state.currentBinary = data;
  document.getElementById("binary-header-meta").innerHTML = `
    <span>Format: <b style="color:var(--neon-cyan)">${escapeHtml(data.format)}</b></span>
    <span>Arch: <b>${escapeHtml(data.arch)}</b></span>
    <span>Size: <b>${formatBytes(data.size_bytes)}</b></span>
    <span>Entropy: <b style="color:${entropyZoneColor(data.overall_entropy)}">${data.overall_entropy}</b></span>
  `;
  drawEntropyCanvas(data.entropy_profile);

  const secBody = document.getElementById("section-tbody");
  secBody.innerHTML = data.sections
    .map(
      (s) => `
      <tr data-jump-offset="${s.offset}" style="cursor:pointer;">
        <td style="color:var(--neon-cyan)">${escapeHtml(s.name)}</td>
        <td>0x${s.offset.toString(16)}</td>
        <td>${escapeHtml(s.virtual_addr)}</td>
        <td>${formatBytes(s.size)}</td>
        <td style="color:${entropyZoneColor(s.entropy)}">${s.entropy}</td>
      </tr>`
    )
    .join("");

  secBody.querySelectorAll("[data-jump-offset]").forEach((tr) => {
    tr.addEventListener("click", async () => {
      const hexWin = await apiJson(`/api/binary/hex?name=${encodeURIComponent(name)}&offset=${tr.dataset.jumpOffset}&length=256`);
      renderHexRows(hexWin);
    });
  });

  document.getElementById("strings-box").innerHTML = data.strings
    .map((st) => `<div class="hex-row"><span class="hex-off">${escapeHtml(st.hex_offset)}</span><span class="hex-bytes">${escapeHtml(st.text)}</span></div>`)
    .join("");

  renderHexRows(data.hex_window);
  await runAobScan();
}

async function runAobScan() {
  const name = document.getElementById("binary-select").value;
  if (!name) return;
  const rawPat = document.getElementById("aob-input").value;
  const normalized = normalizeAobInput(rawPat);
  document.getElementById("aob-input").value = normalized;
  const res = await apiJson("/api/binary/aob-scan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, pattern: normalized }),
  });

  const tbody = document.getElementById("aob-tbody");
  tbody.innerHTML = res.hits.slice(0, 10)
    .map(
      (h) => `
      <tr>
        <td style="color:var(--neon-rose)">${escapeHtml(h.hex_offset)}</td>
        <td>${escapeHtml(h.section)}</td>
        <td style="color:var(--neon-cyan)">${escapeHtml(h.matched_bytes)}</td>
        <td><button class="btn" data-aob-jump="${h.offset}" type="button">Hex →</button></td>
      </tr>`
    )
    .join("") || `<tr><td colspan="4" style="color:var(--text-muted)">No matches for pattern in ${escapeHtml(name)}</td></tr>`;

  tbody.querySelectorAll("[data-aob-jump]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const hexWin = await apiJson(`/api/binary/hex?name=${encodeURIComponent(name)}&offset=${btn.dataset.aobJump}&length=256`);
      renderHexRows(hexWin);
    });
  });
}

async function inspectTexture(name) {
  const data = await apiJson(`/api/textures/inspect?name=${encodeURIComponent(name)}`);
  document.getElementById("texture-diag-bar").innerHTML = `
    <span>Dimensions: <b>${data.width}×${data.height}</b></span>
    <span>Power-of-2: <b style="color:${data.is_power_of_two ? "var(--neon-mint)" : "var(--neon-rose)"}">${data.is_power_of_two ? "YES" : "NO"}</b></span>
    <span>Tangent-Space Normal: <b>${data.normal_diag.likely_tangent_space ? "YES" : "NO"}</b></span>
    <span>Normal Polarity: <b style="color:var(--neon-cyan)">${escapeHtml(data.normal_diag.y_polarity_hint)}</b></span>
    <span>Unit Vector Mean: <b>${data.normal_diag.unit_length_mean}</b></span>
  `;

  const keys = ["COMPOSITE", "R", "G", "B", "A"];
  document.getElementById("texture-channel-grid").innerHTML = keys
    .map((k) => {
      const st = data.channels[k];
      const hist = st ? st.hist16 : [];
      const maxH = Math.max(1, ...hist);
      const bars = hist.map((v) => `<div class="hist-bar" style="height:${Math.round((v / maxH) * 100)}%"></div>`).join("");
      return `
        <div class="channel-card">
          <div style="display:flex;justify-content:space-between;align-items:center;">
            <strong style="font-family:var(--font-mono);color:var(--neon-cyan)">${k === "COMPOSITE" ? "RGBA COMPOSITE" : `CHANNEL ${k}`}</strong>
            <span class="mod-meta">${st ? `μ=${st.mean}` : `${data.width}×${data.height}`}</span>
          </div>
          <img src="${data.previews[k]}" alt="${escapeHtml(k)} channel preview" />
          ${st ? `<div class="hist-bars">${bars}</div><div class="mod-meta"><span>min:${st.min}</span><span>max:${st.max}</span><span>σ:${st.std}</span></div>` : `<div class="mod-meta"><span>Mode: ${escapeHtml(data.original_mode)} (${escapeHtml(data.format)})</span></div>`}
        </div>
      `;
    })
    .join("");
}

async function boot() {
  document.querySelectorAll(".stage-tab").forEach((btn) => {
    btn.addEventListener("click", () => setStage(btn.dataset.stage));
  });

  document.querySelectorAll(".sev-filter").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.sevFilter = btn.dataset.sev;
      document.querySelectorAll(".sev-filter").forEach((b) => b.classList.toggle("active", b === btn));
      renderVfsStage();
    });
  });

  document.getElementById("btn-autosort").addEventListener("click", async () => {
    state.scan = await apiJson("/api/mods/auto-sort", { method: "POST" });
    renderVfsStage();
  });

  document.getElementById("btn-run-merge").addEventListener("click", runThreeWayMerge);
  document.getElementById("btn-aob-scan").addEventListener("click", runAobScan);
  document.getElementById("binary-select").addEventListener("change", (e) => inspectBinary(e.target.value));
  document.getElementById("texture-select").addEventListener("change", (e) => inspectTexture(e.target.value));
  document.getElementById("btn-inspect-tex").addEventListener("click", () => inspectTexture(document.getElementById("texture-select").value));

  state.scan = await apiJson("/api/workspace/scan");
  populateSelectors();
  renderVfsStage();
  await runThreeWayMerge();
  if (state.scan.binaries.length) await inspectBinary(state.scan.binaries[0].name);
  if (state.scan.textures.length) await inspectTexture(state.scan.textures[0].name);
}

boot();
