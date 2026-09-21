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
from ...pipeline.passes.decisions import definition_problems
from ..schemas import (
    InteractiveFragmentCreate,
    InteractiveFragmentReorder,
    InteractiveFragmentUpdate,
    MoodFragmentCreate,
    MoodFragmentUpdate,
)

router = APIRouter()


def _checked_decision_write(payload: dict, existing: dict[str, Any] | None = None) -> dict:
    """Validate a decision write, or blank the decision columns for other types.

    An update is partial, so validation runs on the *merged* row: an author
    editing one criterion must not be told their question is missing, and an
    author changing only ``decision_threshold`` must still be caught by the
    whole-definition rule. A type that is not a decision has its decision columns
    nulled, so switching a fragment away from ``decision`` cannot leave a
    half-configured question behind for a later type switch to resurrect.
    """
    merged = {**(existing or {}), **payload}
    if merged.get("field_type") != DECISION_FIELD_TYPE:
        # Only for a write that actually names the type; a partial update that
        # never mentions field_type leaves an existing decision alone.
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
    # A null still means "not supplied" for every pre-existing field, exactly as
    # before -- those columns are NOT NULL and a client sending an explicit null
    # meant to omit it. The decision columns are the exception: NULL is a real
    # value there (roll mode has no threshold), so an explicitly-null decision
    # column is a write, not an omission.
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
