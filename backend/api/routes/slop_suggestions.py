"""Phrase Bank suggestion routes: read, refresh, accept, dismiss.

Accept is the only path from a suggestion into the phrase bank.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException

from ...database import accept_slop_suggestion, dismiss_slop_suggestion, get_slop_mining_state, list_slop_suggestions
from ...features import slop_suggestions
from ..deps import validate_phrase_group
from ..schemas import SlopSuggestionAccept

router = APIRouter()


@router.get("/api/phrase-bank/suggestions")
async def api_get_slop_suggestions():
    """Stored suggestions and how the last run ended. Reading never starts a run."""
    return {
        "suggestions": await list_slop_suggestions(),
        "refreshing": slop_suggestions.refreshing(),
        "last_run": await get_slop_mining_state(),
    }


@router.post("/api/phrase-bank/suggestions/refresh")
async def api_refresh_slop_suggestions():
    """Start a background run unless one is already in progress."""
    slop_suggestions.refresh()
    return {"refreshing": True}


@router.post("/api/phrase-bank/suggestions/{suggestion_id}/accept")
async def api_accept_slop_suggestion(suggestion_id: int, data: SlopSuggestionAccept):
    """Save a suggestion's pattern, as edited in the regex editor, as a regex phrase group."""
    _variants, pattern = validate_phrase_group("regex", [], data.pattern)
    group_id = await accept_slop_suggestion(suggestion_id, pattern)
    if group_id is None:
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return {"id": group_id, "kind": "regex", "variants": [], "pattern": pattern}


@router.post("/api/phrase-bank/suggestions/{suggestion_id}/dismiss")
async def api_dismiss_slop_suggestion(suggestion_id: int):
    """Drop a suggestion and never suggest its key again."""
    if not await dismiss_slop_suggestion(suggestion_id, datetime.now(UTC).isoformat()):
        raise HTTPException(status_code=404, detail="Suggestion not found")
    return {"ok": True}
