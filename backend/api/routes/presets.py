"""Global preset routes: the sidebar header's preset picker."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import get_active_preset_id, get_settings, list_presets
from ...features import presets
from ..schemas import PresetActive, PresetCreate, PresetRename
from .endpoints import public_settings

router = APIRouter()


def _missing() -> HTTPException:
    return HTTPException(status_code=404, detail="Preset not found")


@router.get("/api/presets")
async def api_list_presets():
    rows = await list_presets()
    return {"presets": [{"id": row["id"], "name": row["name"]} for row in rows], "active_id": await get_active_preset_id()}


@router.post("/api/presets")
async def api_create_preset(data: PresetCreate):
    row = await presets.create_preset(data.name, data.copy_from)
    if row is None:
        raise _missing()
    return {"id": row["id"], "name": row["name"]}


@router.put("/api/presets/active")
async def api_switch_preset(data: PresetActive):
    """Switch presets; answers the settings row the switch left live."""
    if not await presets.switch_preset(data.id):
        raise _missing()
    return public_settings(await get_settings())


@router.patch("/api/presets/{preset_id}")
async def api_rename_preset(preset_id: str, data: PresetRename):
    row = await presets.rename_preset(preset_id, data.name)
    if row is None:
        raise _missing()
    return {"id": row["id"], "name": row["name"]}


@router.delete("/api/presets/{preset_id}")
async def api_delete_preset(preset_id: str):
    """Delete a preset; answers the settings row, which changes when the active preset was deleted."""
    if not await presets.delete_preset(preset_id):
        raise _missing()
    return public_settings(await get_settings())
