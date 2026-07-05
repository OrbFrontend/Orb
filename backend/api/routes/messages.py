"""Message-level routes: fetch, send/continue, edit, branch ops, and the
SSE-streaming regenerate / rewrite endpoints."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from ...core.macros import Macros
from ...database import (
    delete_message_with_descendants,
    get_character_card,
    get_conversation,
    get_message_by_id,
    get_messages,
    get_messages_with_branch_info,
    get_settings,
    get_user_persona,
    switch_to_branch,
    update_message_content,
)
from ...inference import AbortToken, local_ml
from ...pipeline import (
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_super_regenerate,
    handle_turn,
)
from ...pipeline.predicates import resolve_persona_id
from ..deps import _CleanupStreamingResponse, _conversation_stream_lock, _sse_stream
from ..schemas import (
    AutocompleteInput,
    EditMessage,
    MagicRewriteMsg,
    RegenerateMsg,
    SendMessage,
)

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
    """Magic rewrite: runs the full pipeline as a new sibling steered by a user-supplied direction."""
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


@router.post("/api/conversations/{cid}/autocomplete")
async def api_autocomplete(cid: str, data: AutocompleteInput):
    """Predict a short continuation of the user's in-progress draft (CPU, in-process).

    Opt-in: 503 unless the llama-cpp-python extra and the GGUF are present. Uses a
    trimmed context (char/persona names + last few messages), NOT the Director pipeline.
    """
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    settings = await get_settings()
    if not settings.get("local_ml_enabled", {}).get("autocomplete", True):
        raise HTTPException(status_code=503, detail="Autocomplete unavailable: disabled")
    ok, reason = local_ml.available()
    if not ok:
        raise HTTPException(status_code=503, detail=f"Autocomplete unavailable: {reason}")
    if not data.draft.strip():
        return {"completion": ""}

    card_id = conv.get("character_card_id")
    card = await get_character_card(card_id) if card_id else None
    persona_id = resolve_persona_id(conv, card, settings)
    persona = await get_user_persona(persona_id) if persona_id else None
    user_name = (persona or {}).get("name") or settings.get("user_name") or "User"
    char_name = conv.get("character_name") or (card or {}).get("name") or "Character"

    macros = Macros(user=user_name, char=char_name)
    messages = await get_messages(cid)
    recent = [{"role": m["role"], "content": macros.resolve_prompt(m["content"] or "")} for m in messages[-4:]]
    summary = macros.resolve_prompt((card or {}).get("description") or "")
    prompt = local_ml.build_prompt(char_name, user_name, summary, recent, macros.resolve_prompt(data.draft))

    completion = await local_ml.complete(prompt)
    return {"completion": completion}
