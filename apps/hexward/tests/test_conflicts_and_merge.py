"""TDD tests for VFS load-order conflict solver, topological dependency ordering, and 3-way config merger."""

from pathlib import Path
from hexward.conflicts import (
    scan_mod_workspace,
    solve_load_order,
    three_way_merge_ini,
    three_way_merge_json,
)


def test_scan_mod_workspace_detects_collisions_and_dominance(tmp_path: Path):
    base_dir = tmp_path / "base"
    mods_dir = tmp_path / "mods"
    (base_dir / "config").mkdir(parents=True)
    (base_dir / "config" / "combat.ini").write_text("[Damage]\nPlayerMult=1.0\nEnemyHP=100\n")

    m1 = mods_dir / "01_Overhaul"
    (m1 / "config").mkdir(parents=True)
    (m1 / "mod.json").write_text('{"id":"overhaul","name":"Overhaul","requires":[],"order":1}')
    (m1 / "config" / "combat.ini").write_text("[Damage]\nPlayerMult=1.35\nEnemyHP=150\n")
    (m1 / "scripts").mkdir()
    (m1 / "scripts" / "ai.lua").write_text("-- overhaul ai")

    m2 = mods_dir / "02_Ballistics"
    (m2 / "config").mkdir(parents=True)
    (m2 / "mod.json").write_text('{"id":"ballistics","name":"Ballistics","requires":["overhaul"],"order":2}')
    (m2 / "config" / "combat.ini").write_text("[Damage]\nPlayerMult=1.60\nEnemyHP=100\n")

    report = scan_mod_workspace(tmp_path)
    assert len(report["mods"]) == 2
    assert report["summary"]["total_collisions"] == 1
    col = report["collisions"][0]
    assert col["rel_path"] == "config/combat.ini"
    assert col["winner"] == "ballistics"
    assert col["severity"] == "CONFIG"


def test_solve_load_order_respects_dependencies_and_detects_cycles():
    mods = [
        {"id": "patch_c", "requires": ["overhaul_a"], "order": 0},
        {"id": "overhaul_a", "requires": [], "order": 1},
        {"id": "shader_b", "requires": [], "order": 2},
    ]
    solved = solve_load_order(mods)
    assert solved["has_cycle"] is False
    ids = solved["suggested_order"]
    assert ids.index("overhaul_a") < ids.index("patch_c")

    cyclic = [
        {"id": "a", "requires": ["b"], "order": 0},
        {"id": "b", "requires": ["a"], "order": 1},
    ]
    cyc_res = solve_load_order(cyclic)
    assert cyc_res["has_cycle"] is True


def test_three_way_merge_ini_auto_merges_disjoint_and_flags_conflicts():
    base = "[Combat]\nPlayerDmg=10\nEnemyDmg=20\nAmmo=30\n"
    ours = "[Combat]\nPlayerDmg=15\nEnemyDmg=20\nAmmo=45\n"
    theirs = "[Combat]\nPlayerDmg=10\nEnemyDmg=35\nAmmo=60\n"

    res = three_way_merge_ini(base, ours, theirs, resolutions={"Combat.Ammo": "take_theirs"})
    assert res["auto_merged_count"] >= 2
    assert len(res["conflicts"]) == 1
    assert res["conflicts"][0]["key"] == "Combat.Ammo"
    assert res["conflicts"][0]["selected"] == "take_theirs"
    merged_text = res["merged_text"]
    assert "PlayerDmg=15" in merged_text
    assert "EnemyDmg=35" in merged_text
    assert "Ammo=60" in merged_text


def test_three_way_merge_json_handles_nested_keys():
    base = '{"bloom": {"intensity": 0.4, "threshold": 0.8}, "fov": 75}'
    ours = '{"bloom": {"intensity": 0.9, "threshold": 0.8}, "fov": 90}'
    theirs = '{"bloom": {"intensity": 0.4, "threshold": 1.1}, "fov": 105}'

    res = three_way_merge_json(base, ours, theirs, resolutions={"fov": "keep_ours"})
    assert len(res["conflicts"]) == 1
    assert res["conflicts"][0]["key"] == "fov"
    assert res["merged_obj"]["bloom"]["intensity"] == 0.9
    assert res["merged_obj"]["bloom"]["threshold"] == 1.1
    assert res["merged_obj"]["fov"] == 90
