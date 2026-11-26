"""Backup library routes."""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Callable
from typing import Annotated, ParamSpec, TypeVar

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ...core import maintenance_lock
from ...features.backups import engine as backups
from ...workflows import gpu_swap_host
from ..admission import dataset_maintenance
from ..schemas import PresetExportRequest

router = APIRouter()

_P = ParamSpec("_P")
_T = TypeVar("_T")


async def _finish_thread(fn: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Keep maintenance locks and temporary files alive until a worker stops.

    Cancelling a to_thread await does not stop its OS thread. Delay cancellation
    until the worker finishes, including when the request is cancelled again.
    """
    worker = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()  # Retrieve failures even when the request has gone away.
        raise


@router.get("/api/backups")
async def api_list_backups():
    return await asyncio.to_thread(backups.list_library)


@router.post("/api/backups/export")
async def api_export_backup(data: PresetExportRequest):
    async with maintenance_lock():
        try:
            name = await _finish_thread(backups.build_preset, data.domains, data.strip_keys, data.label)
        except backups.PresetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    return {"name": name}


@router.get("/api/backups/{name}/download")
async def api_download_backup(name: str):
    try:
        path = await asyncio.to_thread(backups._library_path, name)
    except backups.PresetError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return FileResponse(path, media_type="application/octet-stream", filename=name)


@router.post("/api/backups/import")
async def api_import_backup(file: Annotated[UploadFile, File(...)]):
    if not file.filename or not file.filename.lower().endswith(".db"):
        raise HTTPException(status_code=400, detail="Only .db backup files are supported")
    label = os.path.splitext(os.path.basename(file.filename))[0]
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        with open(tmp_path, "wb") as target:
            while chunk := await file.read(1024 * 1024):
                target.write(chunk)
        async with maintenance_lock():
            stored = await _finish_thread(backups.ingest_upload, tmp_path, label)
    except backups.PresetError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    return {"name": stored}


@router.post("/api/backups/{name}/apply")
async def api_apply_backup(name: str):
    async with dataset_maintenance(), maintenance_lock():
        try:
            path = backups._library_path(name)
            backup = await _finish_thread(backups.create_snapshot, f"before applying {name}", path)
            summary = await _finish_thread(backups.apply_preset, path)
        except backups.PresetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    # A preset can switch workflows on, and the GPU swap plan is off while they are off.
    gpu_swap_host.invalidate()
    return {"backup": backup, "summary": summary}


@router.post("/api/backups/{name}/restore")
async def api_restore_backup(name: str):
    async with dataset_maintenance(), maintenance_lock():
        try:
            path = backups._library_path(name)
            meta = backups.read_meta(path) or {}
            full = set(meta.get("included_domains") or backups.ALL_DOMAINS) >= set(backups.ALL_DOMAINS)
            backup = await _finish_thread(backups.create_snapshot, "before restore", None if full else path)
            if full:
                await _finish_thread(backups.restore_full, name)
                summary = None
            else:
                summary = await _finish_thread(backups.restore_partial, path)
        except backups.PresetError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
    gpu_swap_host.invalidate()
    return {"backup": backup, "ok": True, "summary": summary}


@router.delete("/api/backups/{name}")
async def api_delete_backup(name: str):
    try:
        await asyncio.to_thread(backups.delete_library_entry, name)
    except backups.PresetError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    return {"ok": True}
