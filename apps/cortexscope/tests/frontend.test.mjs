import test from "node:test";
import assert from "node:assert/strict";

import {
  escapeHtml,
  classifyTokenHeat,
  estimateTokensClient,
  computeBudgetSummary,
  computeSeriesExtent,
  computeWordDiff,
  formatBytes,
} from "../static/js/metrics.js";

test("escapeHtml neutralizes XSS vectors", () => {
  const dirty = `<img src=x onerror="alert('xss')">&`;
  const clean = escapeHtml(dirty);
  assert.equal(clean.includes("<"), false);
  assert.equal(clean.includes(">"), false);
  assert.equal(clean.includes('"'), false);
  assert.equal(clean, "&lt;img src=x onerror=&quot;alert(&#39;xss&#39;)&quot;&gt;&amp;");
});

test("classifyTokenHeat buckets ITL latencies relative to baseline", () => {
  assert.equal(classifyTokenHeat(12, 20, 60).tier, "fast");
  assert.equal(classifyTokenHeat(28, 25, 80).tier, "normal");
  assert.equal(classifyTokenHeat(75, 25, 80).tier, "warm");
  assert.equal(classifyTokenHeat(160, 25, 80).tier, "spike");
});

test("computeBudgetSummary flags pressure thresholds accurately", () => {
  const low = computeBudgetSummary("hello world", "", 4096);
  assert.equal(low.pressure, "low");
  const highText = "word ".repeat(3000);
  const crit = computeBudgetSummary(highText, "", 4096);
  assert.equal(crit.pressure, "critical");
});

test("computeSeriesExtent handles empty and flat arrays without zero span", () => {
  assert.deepEqual(computeSeriesExtent([]), { min: 0, max: 1 });
  const flat = computeSeriesExtent([42, 42, 42]);
  assert.ok(flat.max > flat.min);
  const normal = computeSeriesExtent([10, 50, 25]);
  assert.equal(normal.min, 10);
  assert.equal(normal.max, 50);
});

test("computeWordDiff highlights shared and differing tokens", () => {
  const diff = computeWordDiff("Linux kernel uses CFS", "Linux kernel uses EEVDF");
  assert.equal(diff.similarityPct > 50 && diff.similarityPct < 100, true);
  assert.ok(diff.aHtml.includes("diff-del"));
  assert.ok(diff.bHtml.includes("diff-add"));
  assert.equal(diff.aHtml.includes("<script>"), false);
});

test("formatBytes formats GiB and MiB cleanly", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(512 * 1024 * 1024), "512 MiB");
  assert.equal(formatBytes(2.5 * 1024 ** 3), "2.50 GiB");
});
