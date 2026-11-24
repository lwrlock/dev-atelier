/** Pure helper utilities for HexWard Studio UI. */

export function escapeHtml(str) {
  return String(str ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function formatBytes(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(2)} MB`;
}

export function entropyZoneColor(entropy) {
  const e = Number(entropy) || 0;
  if (e >= 6.8) return "#f43f5e";
  if (e >= 4.2) return "#22d3ee";
  return "#a855f7";
}

export function normalizeAobInput(raw) {
  return String(raw ?? "")
    .trim()
    .replaceAll(",", " ")
    .split(/\s+/)
    .filter(Boolean)
    .map((tok) => (tok === "?" || tok === "??" || tok === "**" ? "??" : tok.toUpperCase()))
    .join(" ");
}

export function moveArrayItem(list, fromIndex, toIndex) {
  const copy = [...list];
  if (fromIndex < 0 || fromIndex >= copy.length || toIndex < 0 || toIndex >= copy.length) {
    return copy;
  }
  const [item] = copy.splice(fromIndex, 1);
  copy.splice(toIndex, 0, item);
  return copy;
}
