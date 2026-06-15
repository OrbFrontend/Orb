"""Mood-fragment and interactive-fragment CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import (
    create_interactive_fragment,
    create_mood_fragment,
    delete_interactive_fragment,
    delete_mood_fragment,
    get_interactive_fragment,
    get_interactive_fragments,
    get_mood_fragment,
    get_mood_fragments,
    update_interactive_fragment,
    update_mood_fragment,
)
from ..schemas import (
    InteractiveFragmentCreate,
    InteractiveFragmentUpdate,
    MoodFragmentCreate,
    MoodFragmentUpdate,
)

router = APIRouter()


# Mood Fragments ──


@router.get("/api/fragments")
async def api_list_mood_fragments():
    return await get_mood_fragments()


@router.post("/api/fragments")
async def api_create_mood_fragment(data: MoodFragmentCreate):
    existing = await get_mood_fragment(data.id)
    if existing:
        raise HTTPException(status_code=400, detail="Mood fragment with this ID already exists")
    return await create_mood_fragment(data.model_dump())


@router.put("/api/fragments/{fid}")
async def api_update_mood_fragment(fid: str, data: MoodFragmentUpdate):
    result = await update_mood_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=404, detail="Mood fragment not found")
    return result


@router.delete("/api/fragments/{fid}")
async def api_delete_mood_fragment(fid: str):
    if not await delete_mood_fragment(fid):
        raise HTTPException(status_code=404, detail="Mood fragment not found or is built-in")
    return {"ok": True}


# Interactive Fragments ──


@router.get("/api/interactive-fragments")
async def api_list_interactive_fragments():
    return await get_interactive_fragments()


@router.post("/api/interactive-fragments")
async def api_create_interactive_fragment(data: InteractiveFragmentCreate):
    existing = await get_interactive_fragment(data.id)
    if existing:
        raise HTTPException(status_code=400, detail="Interactive fragment with this ID already exists")
    result = await create_interactive_fragment(data.model_dump())
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create interactive fragment")
    return result


@router.put("/api/interactive-fragments/{fid}")
async def api_update_interactive_fragment(fid: str, data: InteractiveFragmentUpdate):
    result = await update_interactive_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    return result


@router.delete("/api/interactive-fragments/{fid}")
async def api_delete_interactive_fragment(fid: str):
    if not await delete_interactive_fragment(fid):
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    return {"ok": True}
