import test from "node:test";
import assert from "node:assert/strict";

import {
  escapeHtml,
  formatBytes,
  entropyZoneColor,
  normalizeAobInput,
  moveArrayItem,
} from "../static/js/hexward_utils.js";

test("escapeHtml neutralizes HTML injection characters", () => {
  assert.equal(escapeHtml('<script>"test"&\'x\'</script>'), "&lt;script&gt;&quot;test&quot;&amp;&#39;x&#39;&lt;/script&gt;");
});

test("formatBytes renders readable binary sizes", () => {
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(2048), "2.0 KB");
});

test("entropyZoneColor classifies packed, code, and padding windows", () => {
  assert.equal(entropyZoneColor(7.45), "#f43f5e");
  assert.equal(entropyZoneColor(5.5), "#22d3ee");
  assert.equal(entropyZoneColor(2.1), "#a855f7");
});

test("normalizeAobInput formats lowercase/compact patterns cleanly", () => {
  assert.equal(normalizeAobInput("48 8b ?? fc"), "48 8B ?? FC");
});

test("moveArrayItem reorders load-order array without mutating source", () => {
  const src = ["a", "b", "c"];
  const moved = moveArrayItem(src, 2, 0);
  assert.deepEqual(moved, ["c", "a", "b"]);
  assert.deepEqual(src, ["a", "b", "c"]);
});
