"""TDD integration tests for HexWard FastAPI REST API over live HTTP."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
import socket

import aiohttp
import pytest
import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@asynccontextmanager
async def live_hexward(tmp_path: Path):
    from hexward.app import create_app

    port = _free_port()
    app = create_app(workspace_dir=tmp_path / "workspace")
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    srv.install_signal_handlers = lambda: None
    task = asyncio.create_task(srv.serve())
    while not srv.started:
        if task.done():
            task.result()
        await asyncio.sleep(0.02)
    try:
        async with aiohttp.ClientSession(f"http://127.0.0.1:{port}") as session:
            yield session
    finally:
        srv.should_exit = True
        await task


def test_hexward_api_end_to_end(tmp_path: Path):
    asyncio.run(_verify_api(tmp_path))


async def _verify_api(tmp_path: Path):
    async with live_hexward(tmp_path) as client:
        async with client.get("/api/workspace/scan") as r:
            assert r.status == 200
            scan = await r.json()
        assert len(scan["mods"]) >= 4
        assert len(scan["collisions"]) >= 1
        assert len(scan["binaries"]) >= 2
        assert len(scan["textures"]) >= 2

        mod_ids = [m["id"] for m in scan["mods"]]
        async with client.post("/api/mods/reorder", json={"ordered_ids": list(reversed(mod_ids))}) as r_ord:
            assert r_ord.status == 200
            ord_data = await r_ord.json()
        assert ord_data["mods"][0]["id"] == mod_ids[-1]

        async with client.post(
            "/api/merge/three-way",
            json={"rel_path": "config/combat_balance.ini", "mod_a": mod_ids[0], "mod_b": mod_ids[1], "resolutions": {}},
        ) as r_mrg:
            assert r_mrg.status == 200
            assert "merged_text" in await r_mrg.json()

        bin_name = scan["binaries"][0]["name"]
        async with client.get("/api/binary/dissect", params={"name": bin_name}) as r_bin:
            assert r_bin.status == 200
            assert len((await r_bin.json())["sections"]) >= 2

        async with client.post("/api/binary/aob-scan", json={"name": bin_name, "pattern": "48 8B ?? ??"}) as r_aob:
            assert r_aob.status == 200
            assert "hits" in await r_aob.json()

        tex_name = scan["textures"][0]["name"]
        async with client.get("/api/textures/inspect", params={"name": tex_name}) as r_tex:
            assert r_tex.status == 200
            assert "previews" in await r_tex.json()
            assert r_tex.headers.get("X-Content-Type-Options") == "nosniff"
            assert r_tex.headers.get("X-Frame-Options") == "DENY"

        # Path traversal attempts on 3-way merge & asset dissector must be rejected
        async with client.post(
            "/api/merge/three-way",
            json={"rel_path": "../../etc/passwd", "mod_a": mod_ids[0], "mod_b": mod_ids[1], "resolutions": {}},
        ) as r_trav:
            assert r_trav.status in (400, 403, 404)

        async with client.get("/api/binary/dissect", params={"name": "../mods/01_valkyrie_core/mod.json"}) as r_esc:
            assert r_esc.status in (400, 403, 404)
