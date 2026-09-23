"""Mood-fragment and interactive-fragment CRUD routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from ...core import DECISION_COLUMNS, DECISION_FIELD_TYPE
from ...database import (
    InteractiveFragmentReorderLaneMismatch,
    create_interactive_fragment,
    create_mood_fragment,
    delete_interactive_fragment,
    delete_mood_fragment,
    get_interactive_fragment,
    get_interactive_fragments,
    get_mood_fragment,
    get_mood_fragments,
    reorder_interactive_fragments,
    update_interactive_fragment,
    update_mood_fragment,
)
from ...pipeline.passes.judge import definition_problems
from ..schemas import (
    InteractiveFragmentCreate,
    InteractiveFragmentReorder,
    InteractiveFragmentUpdate,
    MoodFragmentCreate,
    MoodFragmentUpdate,
)

router = APIRouter()


def _checked_decision_write(payload: dict, existing: dict[str, Any] | None = None) -> dict:
    """Validate the merged decision definition and clear its fields on type changes."""
    merged = {**(existing or {}), **payload}
    if merged.get("field_type") != DECISION_FIELD_TYPE:
        # A partial update that omits field_type leaves the existing type alone.
        if "field_type" in payload:
            payload.update({column: None for column in DECISION_COLUMNS})
        return payload
    problems = definition_problems(merged)
    if problems:
        raise HTTPException(status_code=422, detail="; ".join(problems))
    return payload


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
    result = await create_interactive_fragment(_checked_decision_write(data.model_dump()))
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create interactive fragment")
    return result


@router.put("/api/interactive-fragments/reorder")
async def api_reorder_interactive_fragments(data: InteractiveFragmentReorder):
    try:
        reordered = await reorder_interactive_fragments([(item.id, item.sort_order) for item in data.items])
    except InteractiveFragmentReorderLaneMismatch as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not reordered:
        raise HTTPException(status_code=404, detail="One or more interactive fragments no longer exist")
    return {"ok": True}


@router.put("/api/interactive-fragments/{fid}")
async def api_update_interactive_fragment(fid: str, data: InteractiveFragmentUpdate):
    existing = await get_interactive_fragment(fid)
    if not existing:
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    # For legacy fields, null means "not supplied". Decision fields use null to
    # clear values such as the threshold when switching to roll mode.
    payload = {
        key: value for key, value in data.model_dump(exclude_unset=True).items() if value is not None or key in DECISION_COLUMNS
    }
    payload = _checked_decision_write(payload, dict(existing))
    result = await update_interactive_fragment(fid, payload)
    if not result:
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    return result


@router.delete("/api/interactive-fragments/{fid}")
async def api_delete_interactive_fragment(fid: str):
    if not await delete_interactive_fragment(fid):
        raise HTTPException(status_code=404, detail="Interactive fragment not found")
    return {"ok": True}
