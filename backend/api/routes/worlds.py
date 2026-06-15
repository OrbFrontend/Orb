"""Worlds and lorebook-entry CRUD + import routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...database import (
    create_lorebook_entry,
    create_world,
    delete_lorebook_entry,
    delete_world,
    get_active_lorebook_entries,
    get_lorebook_entries,
    get_world,
    get_worlds,
    update_lorebook_entry,
    update_world,
)
from ..deps import _normalise_lorebook_entry, require_lorebook_entry, require_world
from ..schemas import (
    LorebookEntryCreate,
    LorebookEntryUpdate,
    LorebookImportPayload,
    WorldCreate,
    WorldUpdate,
)

router = APIRouter()


# Worlds ──


@router.get("/api/worlds")
async def api_list_worlds():
    return await get_worlds()


@router.post("/api/worlds")
async def api_create_world(data: WorldCreate):
    return await create_world(data.model_dump())


@router.put("/api/worlds/{world_id}")
async def api_update_world(world_id: str, data: WorldUpdate):
    result = await update_world(world_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="World not found")
    return result


@router.delete("/api/worlds/{world_id}")
async def api_delete_world(world_id: str):
    if not await delete_world(world_id):
        raise HTTPException(status_code=404, detail="World not found")
    return {"ok": True}


# Lorebook Entries ──


@router.get("/api/worlds/{world_id}/entries")
async def api_list_lorebook_entries(world: dict = Depends(require_world)):  # noqa: B008
    return await get_lorebook_entries(world["id"])


@router.post("/api/worlds/{world_id}/entries")
async def api_create_lorebook_entry(data: LorebookEntryCreate, world: dict = Depends(require_world)):  # noqa: B008
    return await create_lorebook_entry(world["id"], data.model_dump())


@router.get("/api/worlds/{world_id}/entries/{entry_id}")
async def api_get_lorebook_entry(
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    return entry


@router.put("/api/worlds/{world_id}/entries/{entry_id}")
async def api_update_lorebook_entry(
    data: LorebookEntryUpdate,
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    result = await update_lorebook_entry(entry["id"], data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="Entry not found")
    return result


@router.delete("/api/worlds/{world_id}/entries/{entry_id}")
async def api_delete_lorebook_entry(
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    if not await delete_lorebook_entry(entry["id"]):
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


@router.post("/api/worlds/{world_id}/import")
async def api_import_lorebook(world_id: str, payload: LorebookImportPayload):
    world = await get_world(world_id)
    if not world:
        raise HTTPException(status_code=404, detail="World not found")

    raw_entries = payload.entries
    # Normalise both formats into a flat list of dicts
    if isinstance(raw_entries, dict):
        # SillyTavern standalone: {"0": {...}, "1": {...}}
        items = list(raw_entries.values())
    elif isinstance(raw_entries, list):
        # Tavern V2 character_book: [...]
        items = raw_entries
    else:
        raise HTTPException(status_code=422, detail="entries must be an object or array")

    created = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry_data = _normalise_lorebook_entry(item)
        created.append(await create_lorebook_entry(world_id, entry_data))

    return {"imported": len(created), "entries": created}


@router.get("/api/lorebook-entries/active")
async def api_get_active_lorebook_entries():
    return await get_active_lorebook_entries()
