"""Preset / backup library routes."""

from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ...core import maintenance_lock
from ...features.presets import engine as presets
from ..schemas import PresetExportRequest

router = APIRouter()


@router.get("/api/presets")
async def api_list_presets():
    return await asyncio.to_thread(presets.list_library)


@router.post("/api/presets/export")
async def api_export_preset(data: PresetExportRequest):
    async with maintenance_lock():
        try:
            name = await asyncio.to_thread(presets.build_preset, data.domains, data.strip_keys, data.label)
        except presets.PresetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"name": name}


@router.get("/api/presets/{name}/download")
async def api_download_preset(name: str):
    try:
        path = await asyncio.to_thread(presets._library_path, name)
    except presets.PresetError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return FileResponse(
        path,
        media_type="application/octet-stream",
        filename=name,
    )


@router.post("/api/presets/import")
async def api_import_preset(file: Annotated[UploadFile, File(...)]):
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db preset files are supported")
    label = os.path.splitext(os.path.basename(file.filename))[0]
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = tmp.name
    async with maintenance_lock():
        try:
            stored = await asyncio.to_thread(presets.ingest_upload, tmp_path, label)
        except presets.PresetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    return {"name": stored}


@router.post("/api/presets/{name}/apply")
async def api_apply_preset(name: str):
    async with maintenance_lock():
        try:
            path = presets._library_path(name)
            backup = await asyncio.to_thread(presets.create_snapshot, f"before applying {name}")
            summary = await asyncio.to_thread(presets.apply_preset, path)
        except presets.PresetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"backup": backup, "summary": summary}


@router.post("/api/presets/{name}/restore")
async def api_restore_preset(name: str):
    async with maintenance_lock():
        try:
            path = presets._library_path(name)
            meta = presets.read_meta(path) or {}
            backup = await asyncio.to_thread(presets.create_snapshot, "before restore")
            full = set(meta.get("included_domains") or presets.ALL_DOMAINS) >= set(presets.ALL_DOMAINS)
            if full:
                await asyncio.to_thread(presets.restore_full, name)
                summary = None
            else:
                summary = await asyncio.to_thread(presets.restore_partial, path)
        except presets.PresetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"backup": backup, "ok": True, "summary": summary}


@router.delete("/api/presets/{name}")
async def api_delete_preset(name: str):
    try:
        await asyncio.to_thread(presets.delete_library_entry, name)
    except presets.PresetError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True}
