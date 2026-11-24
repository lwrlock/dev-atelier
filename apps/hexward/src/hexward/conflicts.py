"""VFS Load-Order Conflict Solver, Kahn Dependency Topology, and 3-Way Semantic Config Merger."""

from __future__ import annotations

from collections import defaultdict, deque
import configparser
import copy
import json
from pathlib import Path
from typing import Any


def classify_severity(rel_path: str) -> str:
    """Assign conflict severity based on file extension/domain."""
    low = rel_path.lower()
    if low.endswith((".dll", ".so", ".lua", ".upk", ".pak", ".bin", ".exe", ".py")):
        return "CRITICAL"
    if low.endswith((".ini", ".json", ".cfg", ".toml", ".yaml", ".xml")):
        return "CONFIG"
    if low.endswith((".png", ".dds", ".tga", ".wav", ".ogg")):
        return "ASSET"
    return "INFO"


def _load_mod_meta(mod_dir: Path, fallback_order: int) -> dict[str, Any]:
    meta_path = mod_dir / "mod.json"
    data: dict[str, Any] = {}
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    return {
        "id": str(data.get("id") or mod_dir.name),
        "name": str(data.get("name") or mod_dir.name),
        "author": str(data.get("author") or "Community"),
        "version": str(data.get("version") or "1.0.0"),
        "category": str(data.get("category") or "Gameplay"),
        "requires": list(data.get("requires") or []),
        "order": int(data.get("order", fallback_order)),
        "dir_name": mod_dir.name,
        "enabled": bool(data.get("enabled", True)),
    }


def solve_load_order(mods: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute dependency-safe load order via Kahn topological sort and detect missing/cyclic edges."""
    by_id = {m["id"]: m for m in mods}
    indegree: dict[str, int] = {m["id"]: 0 for m in mods}
    adj: dict[str, list[str]] = defaultdict(list)
    missing_deps: list[dict[str, str]] = []

    for mod in mods:
        mid = mod["id"]
        for dep in mod.get("requires", []):
            if dep not in by_id:
                missing_deps.append({"mod": mid, "missing": dep})
            else:
                adj[dep].append(mid)
                indegree[mid] += 1

    # Preserve original numeric priority among currently eligible zero-indegree nodes
    eligible = sorted([m["id"] for m in mods if indegree[m["id"]] == 0], key=lambda x: by_id[x].get("order", 0))
    queue = deque(eligible)
    ordered: list[str] = []

    while queue:
        cur = queue.popleft()
        ordered.append(cur)
        newly_ready: list[str] = []
        for nxt in adj[cur]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                newly_ready.append(nxt)
        if newly_ready:
            combined = list(queue) + newly_ready
            combined.sort(key=lambda x: by_id[x].get("order", 0))
            queue = deque(combined)

    has_cycle = len(ordered) < len(mods)
    if has_cycle:
        remaining = [m["id"] for m in mods if m["id"] not in set(ordered)]
        ordered.extend(remaining)

    return {
        "suggested_order": ordered,
        "has_cycle": has_cycle,
        "missing_dependencies": missing_deps,
    }


def scan_mod_workspace(workspace_dir: Path | str, ordered_ids: list[str] | None = None) -> dict[str, Any]:
    """Scan base/ and mods/ inside workspace_dir to compute VFS collisions and dominance stats."""
    ws = Path(workspace_dir)
    mods_root = ws / "mods"
    base_root = ws / "base"

    mods: list[dict[str, Any]] = []
    if mods_root.exists():
        mod_dirs = sorted([p for p in mods_root.iterdir() if p.is_dir()])
        for idx, mdir in enumerate(mod_dirs):
            meta = _load_mod_meta(mdir, idx)
            files = sorted(
                [
                    p.relative_to(mdir).as_posix()
                    for p in mdir.rglob("*")
                    if p.is_file() and p.name != "mod.json"
                ]
            )
            meta["files"] = files
            meta["file_count"] = len(files)
            mods.append(meta)

    if ordered_ids:
        rank = {mid: i for i, mid in enumerate(ordered_ids)}
        mods.sort(key=lambda m: rank.get(m["id"], 9999 + m["order"]))
    else:
        mods.sort(key=lambda m: (m["order"], m["id"]))

    for idx, m in enumerate(mods):
        m["priority"] = idx

    base_files = set()
    if base_root.exists():
        base_files = {p.relative_to(base_root).as_posix() for p in base_root.rglob("*") if p.is_file()}

    # Build VFS map: later priority wins
    vfs_owners: dict[str, list[str]] = defaultdict(list)
    for m in mods:
        if not m["enabled"]:
            continue
        for rel in m["files"]:
            vfs_owners[rel].append(m["id"])

    collisions: list[dict[str, Any]] = []
    win_counts: dict[str, int] = defaultdict(int)
    overridden_counts: dict[str, int] = defaultdict(int)

    for rel, providers in sorted(vfs_owners.items()):
        winner = providers[-1]
        win_counts[winner] += 1
        if len(providers) > 1:
            for loser in providers[:-1]:
                overridden_counts[loser] += 1
            collisions.append(
                {
                    "rel_path": rel,
                    "severity": classify_severity(rel),
                    "providers": providers,
                    "winner": winner,
                    "losers": providers[:-1],
                    "has_base": rel in base_files,
                }
            )

    for m in mods:
        mid = m["id"]
        total = max(1, m["file_count"])
        m["winning_files"] = win_counts[mid]
        m["overridden_files"] = overridden_counts[mid]
        m["dominance_pct"] = round(100.0 * win_counts[mid] / total, 1)

    topo = solve_load_order(mods)
    return {
        "mods": mods,
        "collisions": collisions,
        "topology": topo,
        "base_files": sorted(base_files),
        "summary": {
            "mod_count": len(mods),
            "vfs_total_files": len(vfs_owners),
            "total_collisions": len(collisions),
            "critical_collisions": sum(1 for c in collisions if c["severity"] == "CRITICAL"),
            "config_collisions": sum(1 for c in collisions if c["severity"] == "CONFIG"),
            "has_cycle": topo["has_cycle"],
        },
    }


def _parse_ini(text: str) -> dict[str, dict[str, str]]:
    cp = configparser.ConfigParser(interpolation=None)
    cp.optionxform = str  # preserve exact casing
    cp.read_string(text or "")
    out: dict[str, dict[str, str]] = {}
    if cp.defaults():
        out["DEFAULT"] = dict(cp.defaults())
    for sec in cp.sections():
        out[sec] = {k: v for k, v in cp.items(sec, raw=True)}
    return out


def _render_ini(mapping: dict[str, dict[str, str]]) -> str:
    lines: list[str] = []
    for sec, kv in mapping.items():
        if sec != "DEFAULT":
            lines.append(f"[{sec}]")
        for k, v in kv.items():
            lines.append(f"{k}={v}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def three_way_merge_ini(
    base_text: str,
    ours_text: str,
    theirs_text: str,
    resolutions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform key-level 3-way INI merge with per-key conflict resolution."""
    res_map = resolutions or {}
    base = _parse_ini(base_text)
    ours = _parse_ini(ours_text)
    theirs = _parse_ini(theirs_text)

    all_secs = list(dict.fromkeys([*base.keys(), *ours.keys(), *theirs.keys()]))
    merged: dict[str, dict[str, str]] = {}
    conflicts: list[dict[str, Any]] = []
    auto_merged_count = 0

    for sec in all_secs:
        b_sec = base.get(sec, {})
        o_sec = ours.get(sec, {})
        t_sec = theirs.get(sec, {})
        all_keys = list(dict.fromkeys([*b_sec.keys(), *o_sec.keys(), *t_sec.keys()]))
        merged[sec] = {}

        for key in all_keys:
            bv = b_sec.get(key)
            ov = o_sec.get(key)
            tv = t_sec.get(key)
            fqk = f"{sec}.{key}"

            if ov == tv:
                if ov is not None:
                    merged[sec][key] = ov
            elif ov == bv:
                if tv is not None:
                    merged[sec][key] = tv
                auto_merged_count += 1
            elif tv == bv:
                if ov is not None:
                    merged[sec][key] = ov
                auto_merged_count += 1
            else:
                choice = res_map.get(fqk, "keep_ours")
                if choice == "take_theirs":
                    chosen_val = tv if tv is not None else ""
                elif choice == "keep_base":
                    chosen_val = bv if bv is not None else ""
                elif choice.startswith("custom:"):
                    chosen_val = choice[7:]
                else:
                    choice = "keep_ours"
                    chosen_val = ov if ov is not None else ""
                merged[sec][key] = chosen_val
                conflicts.append(
                    {
                        "key": fqk,
                        "base": bv,
                        "ours": ov,
                        "theirs": tv,
                        "selected": choice,
                        "resolved_value": chosen_val,
                    }
                )

    return {
        "format": "ini",
        "auto_merged_count": auto_merged_count,
        "conflicts": conflicts,
        "merged_text": _render_ini(merged),
    }


def _flatten_json(obj: Any, prefix: str = "") -> dict[str, Any]:
    out: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, dict):
                out.update(_flatten_json(v, path))
            else:
                out[path] = v
    return out


def _unflatten_json(flat: dict[str, Any]) -> dict[str, Any]:
    root: dict[str, Any] = {}
    for path, val in flat.items():
        parts = path.split(".")
        cur = root
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = val
    return root


def three_way_merge_json(
    base_text: str,
    ours_text: str,
    theirs_text: str,
    resolutions: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Perform leaf-path 3-way JSON merge with conflict surfacing and resolution."""
    res_map = resolutions or {}
    base_flat = _flatten_json(json.loads(base_text or "{}"))
    ours_flat = _flatten_json(json.loads(ours_text or "{}"))
    theirs_flat = _flatten_json(json.loads(theirs_text or "{}"))

    all_keys = list(dict.fromkeys([*base_flat.keys(), *ours_flat.keys(), *theirs_flat.keys()]))
    merged_flat: dict[str, Any] = {}
    conflicts: list[dict[str, Any]] = []
    auto_merged_count = 0

    _SENTINEL = object()

    for key in all_keys:
        bv = base_flat.get(key, _SENTINEL)
        ov = ours_flat.get(key, _SENTINEL)
        tv = theirs_flat.get(key, _SENTINEL)

        if ov == tv:
            if ov is not _SENTINEL:
                merged_flat[key] = copy.deepcopy(ov)
        elif ov == bv:
            if tv is not _SENTINEL:
                merged_flat[key] = copy.deepcopy(tv)
            auto_merged_count += 1
        elif tv == bv:
            if ov is not _SENTINEL:
                merged_flat[key] = copy.deepcopy(ov)
            auto_merged_count += 1
        else:
            choice = res_map.get(key, "keep_ours")
            if choice == "take_theirs":
                chosen = None if tv is _SENTINEL else tv
            elif choice == "keep_base":
                chosen = None if bv is _SENTINEL else bv
            else:
                choice = "keep_ours"
                chosen = None if ov is _SENTINEL else ov
            if chosen is not None:
                merged_flat[key] = copy.deepcopy(chosen)
            conflicts.append(
                {
                    "key": key,
                    "base": None if bv is _SENTINEL else bv,
                    "ours": None if ov is _SENTINEL else ov,
                    "theirs": None if tv is _SENTINEL else tv,
                    "selected": choice,
                    "resolved_value": chosen,
                }
            )

    merged_obj = _unflatten_json(merged_flat)
    return {
        "format": "json",
        "auto_merged_count": auto_merged_count,
        "conflicts": conflicts,
        "merged_obj": merged_obj,
        "merged_text": json.dumps(merged_obj, indent=2) + "\n",
    }
