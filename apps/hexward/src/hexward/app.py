"""FastAPI application for HexWard Modding Conflict Solver & Binary Dissector."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .conflicts import scan_mod_workspace, three_way_merge_ini, three_way_merge_json
from .dissector import dissect_binary, render_hex_window, scan_aob_pattern
from .seeds import ensure_sample_workspace
from .textures import inspect_texture_bytes


class ReorderReq(BaseModel):
    ordered_ids: list[str] = Field(min_length=1, max_length=128)


class ThreeWayMergeReq(BaseModel):
    rel_path: str = Field(min_length=1, max_length=256)
    mod_a: str = Field(min_length=1, max_length=128)
    mod_b: str = Field(min_length=1, max_length=128)
    resolutions: dict[str, str] = Field(default_factory=dict)


class AobScanReq(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    pattern: str = Field(min_length=2, max_length=256)


def _static_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "static"


def default_workspace_dir() -> Path:
    override = os.environ.get("HEXWARD_WORKSPACE")
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "hexward" / "workspace"


def _safe_resolve(root: Path, name: str) -> Path:
    root_resolved = root.resolve()
    target = (root_resolved / name).resolve()
    if not target.is_relative_to(root_resolved) or not target.is_file():
        raise HTTPException(status_code=404, detail=f"Asset not found: {name}")
    return target


def _safe_rel_path(root: Path, rel_path: str) -> Path:
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise HTTPException(status_code=400, detail="Invalid relative workspace path")
    root_resolved = root.resolve()
    target = (root_resolved / rel).resolve()
    if not target.is_relative_to(root_resolved):
        raise HTTPException(status_code=403, detail="Workspace path escapes sandbox root")
    return target


def create_app(*, workspace_dir: Path | str | None = None) -> FastAPI:
    ws = ensure_sample_workspace(Path(workspace_dir) if workspace_dir else default_workspace_dir())
    app = FastAPI(title="HexWard", version="0.1.0")
    app.state.workspace_dir = ws
    app.state.custom_order: list[str] | None = None

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    sdir = _static_dir()
    if sdir.exists():
        app.mount("/static", StaticFiles(directory=str(sdir)), name="static")

    def _list_assets(sub: str) -> list[dict[str, Any]]:
        folder = ws / sub
        if not folder.exists():
            return []
        return [
            {"name": p.name, "size_bytes": p.stat().st_size, "ext": p.suffix.lower()}
            for p in sorted(folder.iterdir())
            if p.is_file()
        ]

    @app.get("/")
    async def index() -> Response:
        idx = sdir / "index.html"
        if idx.exists():
            return FileResponse(idx)
        return JSONResponse({"service": "HexWard", "status": "ok"})

    @app.get("/api/workspace/scan")
    async def workspace_scan() -> dict[str, Any]:
        scan = scan_mod_workspace(ws, ordered_ids=app.state.custom_order)
        scan["binaries"] = _list_assets("binaries")
        scan["textures"] = _list_assets("textures")
        return scan

    @app.post("/api/mods/reorder")
    async def mods_reorder(req: ReorderReq) -> dict[str, Any]:
        app.state.custom_order = list(req.ordered_ids)
        scan = scan_mod_workspace(ws, ordered_ids=app.state.custom_order)
        scan["binaries"] = _list_assets("binaries")
        scan["textures"] = _list_assets("textures")
        return scan

    @app.post("/api/mods/auto-sort")
    async def mods_auto_sort() -> dict[str, Any]:
        cur = scan_mod_workspace(ws, ordered_ids=app.state.custom_order)
        app.state.custom_order = cur["topology"]["suggested_order"]
        scan = scan_mod_workspace(ws, ordered_ids=app.state.custom_order)
        scan["binaries"] = _list_assets("binaries")
        scan["textures"] = _list_assets("textures")
        return scan

    @app.post("/api/merge/three-way")
    async def merge_three_way(req: ThreeWayMergeReq) -> dict[str, Any]:
        scan = scan_mod_workspace(ws, ordered_ids=app.state.custom_order)
        dir_map = {m["id"]: m["dir_name"] for m in scan["mods"]}
        if req.mod_a not in dir_map or req.mod_b not in dir_map:
            raise HTTPException(status_code=404, detail="Mod ID not found")

        base_file = _safe_rel_path(ws / "base", req.rel_path)
        a_file = _safe_rel_path(ws / "mods" / dir_map[req.mod_a], req.rel_path)
        b_file = _safe_rel_path(ws / "mods" / dir_map[req.mod_b], req.rel_path)

        base_txt = base_file.read_text(encoding="utf-8") if base_file.is_file() else ""
        a_txt = a_file.read_text(encoding="utf-8") if a_file.is_file() else ""
        b_txt = b_file.read_text(encoding="utf-8") if b_file.is_file() else ""

        if req.rel_path.lower().endswith(".json"):
            res = three_way_merge_json(base_txt or "{}", a_txt or "{}", b_txt or "{}", req.resolutions)
        else:
            res = three_way_merge_ini(base_txt, a_txt, b_txt, req.resolutions)
        res.update({"rel_path": req.rel_path, "mod_a": req.mod_a, "mod_b": req.mod_b, "base_text": base_txt, "ours_text": a_txt, "theirs_text": b_txt})
        return res

    @app.get("/api/binary/dissect")
    async def binary_dissect(name: str = Query(...)) -> dict[str, Any]:
        fp = _safe_resolve(ws / "binaries", name)
        blob = fp.read_bytes()
        info = dissect_binary(blob, filename=name)
        info["hex_window"] = render_hex_window(blob, offset=0, length=256)
        return info

    @app.get("/api/binary/hex")
    async def binary_hex(name: str = Query(...), offset: int = Query(0, ge=0), length: int = Query(256, ge=16, le=1024)) -> dict[str, Any]:
        fp = _safe_resolve(ws / "binaries", name)
        return render_hex_window(fp.read_bytes(), offset=offset, length=length)

    @app.post("/api/binary/aob-scan")
    async def binary_aob_scan(req: AobScanReq) -> dict[str, Any]:
        fp = _safe_resolve(ws / "binaries", req.name)
        blob = fp.read_bytes()
        info = dissect_binary(blob, filename=req.name)
        try:
            hits = scan_aob_pattern(blob, req.pattern, sections=info["sections"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"name": req.name, "pattern": req.pattern, "hit_count": len(hits), "hits": hits}

    @app.get("/api/textures/inspect")
    async def texture_inspect(name: str = Query(...)) -> dict[str, Any]:
        fp = _safe_resolve(ws / "textures", name)
        return inspect_texture_bytes(fp.read_bytes(), filename=name)

    @app.get("/api/report/load-order")
    async def export_load_order() -> Response:
        scan = scan_mod_workspace(ws, ordered_ids=app.state.custom_order)
        payload = {
            "ordered_mods": [
                {"priority": m["priority"], "id": m["id"], "name": m["name"], "version": m["version"]}
                for m in scan["mods"]
            ],
            "summary": scan["summary"],
        }
        return Response(
            content=json.dumps(payload, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="hexward-load-order.json"'},
        )

    return app
