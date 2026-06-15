"""Phrase-bank CRUD routes."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from ...database import (
    add_phrase_group,
    delete_phrase_group,
    get_phrase_bank_rows,
    update_phrase_group,
)
from ..deps import _validate_phrase_group
from ..schemas import PhraseGroupCreate, PhraseGroupUpdate

router = APIRouter()


@router.get("/api/phrase-bank")
async def api_get_phrase_bank():
    """Return phrase bank rows with ids for UI management."""
    return await get_phrase_bank_rows()


@router.post("/api/phrase-bank")
async def api_create_phrase_group(data: PhraseGroupCreate):
    """Create a new phrase group (literal variants or a single regex)."""
    variants, pattern = _validate_phrase_group(data.kind, data.variants, data.pattern)
    group_id = await add_phrase_group(variants, data.kind, pattern)
    return {"id": group_id, "kind": data.kind, "variants": variants, "pattern": pattern}


@router.put("/api/phrase-bank/{group_id}")
async def api_update_phrase_group(group_id: int, data: PhraseGroupUpdate):
    """Update an existing phrase group (literal variants or a single regex)."""
    variants, pattern = _validate_phrase_group(data.kind, data.variants, data.pattern)
    success = await update_phrase_group(group_id, variants, data.kind, pattern)
    if not success:
        raise HTTPException(status_code=404, detail="Phrase group not found")
    return {"ok": True, "id": group_id, "kind": data.kind, "variants": variants, "pattern": pattern}


@router.delete("/api/phrase-bank/{group_id}")
async def api_delete_phrase_group(group_id: int):
    """Delete a phrase variant group."""
    success = await delete_phrase_group(group_id)
    if not success:
        raise HTTPException(status_code=404, detail="Phrase group not found")
    return {"ok": True}
