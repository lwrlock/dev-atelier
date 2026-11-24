/**
 * Pure telemetry math, XSS escaping, diffing, and formatting utilities.
 */

const HTML_ESCAPE_MAP = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function escapeHtml(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"']/g, (ch) => HTML_ESCAPE_MAP[ch]);
}

export function classifyTokenHeat(deltaMs, p50Ms = 25, p95Ms = 80) {
  const d = Number(deltaMs) || 0;
  const base = Math.max(8, Number(p50Ms) || 25);
  const warn = Math.max(base * 2.2, Number(p95Ms) || 80);

  if (d <= base * 0.85) {
    return { tier: "fast", color: "#22D3EE", bg: "rgba(34, 211, 238, 0.14)", border: "rgba(34, 211, 238, 0.38)" };
  }
  if (d <= base * 1.8) {
    return { tier: "normal", color: "#94A3B8", bg: "rgba(148, 163, 184, 0.10)", border: "rgba(148, 163, 184, 0.25)" };
  }
  if (d <= warn * 1.25) {
    return { tier: "warm", color: "#F59E0B", bg: "rgba(245, 158, 11, 0.18)", border: "rgba(245, 158, 11, 0.45)" };
  }
  return { tier: "spike", color: "#EF4444", bg: "rgba(239, 68, 68, 0.22)", border: "rgba(239, 68, 68, 0.6)" };
}

export function estimateTokensClient(text) {
  if (!text) return 0;
  const trimmed = text.trim();
  if (!trimmed) return 0;
  const byChars = Math.ceil(text.length / 3.6);
  const byWords = Math.ceil(trimmed.split(/\s+/).length * 1.32);
  return Math.max(byChars, byWords);
}

export function computeBudgetSummary(prompt = "", systemPrompt = "", contextLength = 4096) {
  const ctx = Math.max(1, Number(contextLength) || 4096);
  const tokens = estimateTokensClient(prompt) + estimateTokensClient(systemPrompt);
  const utilPct = Math.min(999.9, Number(((tokens / ctx) * 100).toFixed(2)));
  let pressure = "low";
  if (utilPct >= 95) pressure = "critical";
  else if (utilPct >= 80) pressure = "high";
  else if (utilPct >= 50) pressure = "moderate";

  return {
    tokens,
    contextLength: ctx,
    remaining: Math.max(0, ctx - tokens),
    utilPct,
    pressure,
  };
}

export function computeSeriesExtent(values) {
  const clean = (values || []).filter((v) => typeof v === "number" && Number.isFinite(v));
  if (clean.length === 0) return { min: 0, max: 1 };
  let min = clean[0];
  let max = clean[0];
  for (const v of clean) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  if (max === min) {
    const pad = Math.max(1, Math.abs(min) * 0.1);
    return { min: min - pad, max: max + pad };
  }
  return { min, max };
}

export function formatBytes(bytes) {
  const b = Number(bytes) || 0;
  if (b <= 0) return "0 B";
  const gib = 1024 ** 3;
  const mib = 1024 ** 2;
  if (b >= gib) return `${(b / gib).toFixed(2)} GiB`;
  if (b >= mib) return `${Math.round(b / mib)} MiB`;
  return `${Math.round(b / 1024)} KiB`;
}

export function formatMs(ms) {
  const v = Number(ms);
  if (!Number.isFinite(v)) return "—";
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`;
  return `${v.toFixed(1)} ms`;
}

export function computeWordDiff(textA = "", textB = "") {
  const wordsA = textA.trim() ? textA.trim().split(/\s+/).slice(0, 260) : [];
  const wordsB = textB.trim() ? textB.trim().split(/\s+/).slice(0, 260) : [];
  const m = wordsA.length;
  const n = wordsB.length;
  if (m === 0 && n === 0) {
    return { similarityPct: 100, aHtml: "", bHtml: "" };
  }

  // LCS DP table for concise side-by-side diff
  const dp = Array.from({ length: m + 1 }, () => new Uint16Array(n + 1));
  for (let i = m - 1; i >= 0; i--) {
    for (let j = n - 1; j >= 0; j--) {
      dp[i][j] = wordsA[i] === wordsB[j] ? 1 + dp[i + 1][j + 1] : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }

  const aOut = [];
  const bOut = [];
  let i = 0;
  let j = 0;
  let matches = 0;
  while (i < m && j < n) {
    if (wordsA[i] === wordsB[j]) {
      const safe = escapeHtml(wordsA[i]);
      aOut.push(safe);
      bOut.push(safe);
      matches++;
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      aOut.push(`<mark class="diff-del">${escapeHtml(wordsA[i])}</mark>`);
      i++;
    } else {
      bOut.push(`<mark class="diff-add">${escapeHtml(wordsB[j])}</mark>`);
      j++;
    }
  }
  while (i < m) aOut.push(`<mark class="diff-del">${escapeHtml(wordsA[i++])}</mark>`);
  while (j < n) bOut.push(`<mark class="diff-add">${escapeHtml(wordsB[j++])}</mark>`);

  const denom = Math.max(m, n, 1);
  return {
    similarityPct: Math.round((matches / denom) * 100),
    aHtml: aOut.join(" "),
    bHtml: bOut.join(" "),
  };
}
