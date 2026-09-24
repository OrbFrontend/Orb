"""State-panel routes: a branch's state-fragment state, its history, and manual edits."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ...core import StateOp
from ...database.models import ConversationRow
from ...features.fragment_state import (
    StateWriteError,
    apply_manual_op,
    delete_orphaned_state,
    state_history,
    state_panel,
)
from ..deps import require_conversation, stream_idle_lock
from ..schemas import StateOperation

router = APIRouter()

_BUSY = "A reply is being generated. Edit the state once it finishes."


def _http_error(error: StateWriteError) -> HTTPException:
    return HTTPException(status_code=error.status, detail=str(error))


@router.get("/api/conversations/{cid}/state")
async def api_get_state(cid: str, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    try:
        return await state_panel(cid)
    except StateWriteError as error:
        raise _http_error(error) from error


@router.get("/api/conversations/{cid}/state/history")
async def api_get_state_history(
    cid: str,
    fragment_id: str,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    return await state_history(cid, fragment_id)


@router.post("/api/conversations/{cid}/state")
async def api_apply_state_operation(
    cid: str,
    data: StateOperation,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Apply one manual operation on the active branch, anchored to its leaf.

    Serialized against generation on the server: a write that arrives while a
    reply streams is rejected as busy rather than landing mid-turn, where the
    turn's own changes -- validated against the state it started from -- would
    commit on top of it.
    """
    async with stream_idle_lock(cid) as idle:
        if not idle:
            raise HTTPException(status_code=409, detail=_BUSY)
        try:
            events = await apply_manual_op(cid, StateOp(data.op, data.fragment_id, text=data.text, entry_id=data.entry_id))
        except StateWriteError as error:
            raise _http_error(error) from error
    return {"changes": events, "state": await state_panel(cid)}


@router.delete("/api/conversations/{cid}/state/{fragment_id}")
async def api_delete_orphaned_state(
    cid: str,
    fragment_id: str,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Delete a deleted fragment's saved state from this conversation, on every branch."""
    async with stream_idle_lock(cid) as idle:
        if not idle:
            raise HTTPException(status_code=409, detail=_BUSY)
        try:
            deleted = await delete_orphaned_state(cid, fragment_id)
        except StateWriteError as error:
            raise _http_error(error) from error
    return {"deleted": deleted}
