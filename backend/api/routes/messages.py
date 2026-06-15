"""Message-level routes: fetch, send/continue, edit, branch ops, and the
SSE-streaming regenerate / rewrite endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ...database import (
    delete_message_with_descendants,
    get_conversation,
    get_message_by_id,
    get_messages,
    get_messages_with_branch_info,
    switch_to_branch,
    update_message_content,
)
from ...inference import AbortToken
from ...pipeline import (
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_super_regenerate,
    handle_turn,
)
from ..deps import _CleanupStreamingResponse, _conversation_stream_lock, _sse_stream
from ..schemas import EditMessage, MagicRewriteMsg, RegenerateMsg, SendMessage

router = APIRouter()


@router.get("/api/conversations/{cid}/messages")
async def api_get_messages(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await get_messages_with_branch_info(cid)


@router.post("/api/conversations/{cid}/messages/{msg_id}/edit")
async def api_edit_message(cid: str, msg_id: int, data: EditMessage):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Serialize against an in-flight streaming pipeline on this cid: the
    # pipeline reads message content into the LLM prefix early and persists
    # the assistant reply late, so a mid-stream edit would make the on-disk
    # user message disagree with the prefix that produced the reply.
    async with _conversation_stream_lock(cid):
        original = await get_message_by_id(msg_id)
        if not original or original["conversation_id"] != cid:
            raise HTTPException(status_code=404, detail="Message not found")

        await update_message_content(msg_id, data.content)
        return {"ok": True}


@router.post("/api/conversations/{cid}/messages/{msg_id}/fork-edit")
async def api_fork_edit_message(cid: str, msg_id: int, data: EditMessage, request: Request):
    """Fork at a user message: persist an edited sibling and stream a fresh reply."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    abort_token = AbortToken()
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_fork_edit(cid, msg_id, data.content, abort_token=abort_token),
            request,
            abort_token=abort_token,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


@router.delete("/api/conversations/{cid}/messages/{msg_id}")
async def api_delete_message(cid: str, msg_id: int):
    """Delete a message and all its descendants. Returns updated message list."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Serialize against an in-flight streaming pipeline on this cid: ON
    # DELETE CASCADE on messages.parent_id would otherwise wipe the
    # in-flight assistant row mid-INSERT (IntegrityError) or right after
    # commit (silent disappearance).
    async with _conversation_stream_lock(cid):
        if not await delete_message_with_descendants(cid, msg_id):
            raise HTTPException(status_code=404, detail="Message not found")
        return await get_messages_with_branch_info(cid)


@router.post("/api/conversations/{cid}/messages/{msg_id}/switch-branch")
async def api_switch_branch(cid: str, msg_id: int):
    """Switch to the branch containing msg_id (sets active leaf to deepest descendant)."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Serialize against an in-flight streaming pipeline on this cid: the
    # pipeline's terminal set_active_leaf would otherwise overwrite the
    # branch the user just selected.
    async with _conversation_stream_lock(cid):
        success = await switch_to_branch(cid, msg_id)
        if not success:
            raise HTTPException(status_code=404, detail="Message not found")
        return await get_messages_with_branch_info(cid)


@router.post("/api/conversations/{cid}/messages/{msg_id}/regenerate")
async def api_regenerate_msg(cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None):
    """Regenerate a specific assistant message as a new sibling branch."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    abort_token = AbortToken()
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_regenerate(cid, msg_id, abort_token=abort_token),
            request,
            abort_token=abort_token,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


@router.post("/api/conversations/{cid}/messages/{msg_id}/super_regenerate")
async def api_super_regenerate_msg(cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None):
    """Super-regenerate: keeps prior response as context, asks model for a different direction."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    abort_token = AbortToken()
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_super_regenerate(cid, msg_id, abort_token=abort_token),
            request,
            abort_token=abort_token,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


@router.post("/api/conversations/{cid}/messages/{msg_id}/magic_rewrite")
async def api_magic_rewrite_msg(cid: str, msg_id: int, request: Request, data: MagicRewriteMsg):
    """Magic rewrite: calls the LLM directly with a user-supplied direction, no agent passes."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    abort_token = AbortToken()
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_magic_rewrite(cid, msg_id, data.direction, abort_token=abort_token),
            request,
            abort_token=abort_token,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


@router.post("/api/conversations/{cid}/send")
async def api_send_message(cid: str, data: SendMessage, request: Request):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    attachments = [a.model_dump() for a in data.attachments]
    abort_token = AbortToken()
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_turn(cid, data.content, attachments=attachments, abort_token=abort_token),
            request,
            abort_token=abort_token,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


@router.post("/api/conversations/{cid}/continue")
async def api_continue_from_user(cid: str, request: Request, data: Optional[RegenerateMsg] = None):
    """Generate an assistant response for the current user turn without creating a new message."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await get_messages(cid)
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="Last message is not a user message")
    user_content = messages[-1]["content"]
    abort_token = AbortToken()
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_turn(cid, user_content, skip_user_persist=True, abort_token=abort_token),
            request,
            abort_token=abort_token,
            cid=cid,
        ),
        media_type="text/event-stream",
    )
