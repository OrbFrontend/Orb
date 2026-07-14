"""Conversation lifecycle, summarize/compress/checkpoint, context-size,
stop, and Inspector (director / logs / director-log) routes."""

from __future__ import annotations

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core import estimate_tokens, scrub_log
from ...database import (
    add_conversation_log,
    add_message,
    create_conversation,
    create_direction_notes,
    delete_conversation,
    delete_direction_note,
    direction_note_projection,
    fork_conversation,
    get_active_lorebook_entries,
    get_character_card,
    get_conversation,
    get_conversation_logs,
    get_direction_notes_for_message,
    get_direction_notes_for_path,
    get_director_log_for_message,
    get_director_state,
    get_interactive_fragments,
    get_message_by_id,
    get_messages,
    get_messages_with_branch_info,
    get_mood_fragments,
    get_settings,
    get_user_persona,
    insert_alternate_greeting_swipes,
    list_conversations,
    resolve_char_context,
    set_active_leaf,
    touch_conversation,
    update_conversation,
    update_direction_note,
    update_director_state,
    user_attachment_payloads,
)
from ...database.models import ConversationRow
from ...features import lorebook
from ...features.summarization import ConversationSummarizer
from ...inference import AbortToken, client_from_settings, prompt_builder
from ...pipeline import agent_enabled, persona_macros, resolve_card_and_persona
from ..deps import (
    _active_aborts,
    _CleanupStreamingResponse,
    _sse_stream,
    require_conversation,
)
from ..schemas import (
    CheckpointRequest,
    CompressRequest,
    ConversationCreate,
    ConversationUpdate,
    DirectionNoteCreate,
    DirectionNoteUpdate,
    SummarizeRequest,
)

logger = logging.getLogger(__name__)

# Sentinel interactive_fragment_id stamped on user-authored direction notes; the model's
# record_direction_note step only ever emits real fragment ids, so this never collides with
# one. The frontend keys its distinct styling on the same value -- keep the two in sync.
_USER_NOTE_FRAGMENT_ID = "human"

router = APIRouter()


@router.get("/api/conversations")
async def api_list_conversations():
    return await list_conversations()


@router.post("/api/conversations")
async def api_create_conversation(data: ConversationCreate):
    cid = str(uuid.uuid4())

    char_name = data.character_name
    char_scenario = data.character_scenario
    first_mes = data.first_mes
    post_hist = data.post_history_instructions
    card_id = data.character_card_id
    title = data.title

    # If a character card is specified, pull fields from it
    if card_id:
        card = await get_character_card(card_id)
        if not card:
            raise HTTPException(status_code=404, detail="Character card not found")
        char_name = card.get("name", "")
        char_scenario = card.get("scenario", "")
        first_mes = card.get("first_mes", "")
        post_hist = card.get("post_history_instructions", "")
        if title == "New Conversation":
            title = char_name

    conv = await create_conversation(
        cid=cid,
        title=title,
        char_name=char_name,
        char_scenario=char_scenario,
        post_history_instructions=post_hist,
        character_card_id=card_id,
    )

    # If there's a first message, auto-add it as the first assistant turn
    if first_mes.strip():
        msg_id, _ = await add_message(cid, "assistant", first_mes.strip(), 0, attachments=None)
        await set_active_leaf(cid, msg_id)

        # If we have a character card with alternate greetings, create swipe versions
        if card_id:
            card = await get_character_card(card_id)
            if card:
                alternate_greetings = card.get("alternate_greetings", [])
                count = await insert_alternate_greeting_swipes(cid, alternate_greetings)
                if count:
                    logger.info(f"Created {count} alternate greeting swipes for conversation {cid}")

    return conv


@router.delete("/api/conversations/{cid}")
async def api_delete_conversation(cid: str):
    if not await delete_conversation(cid):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True}


@router.post("/api/conversations/{cid}/touch")
async def api_touch_conversation(cid: str):
    if not await touch_conversation(cid):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True}


@router.put("/api/conversations/{cid}")
async def api_update_conversation(
    cid: str,
    data: ConversationUpdate,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    update_data = data.model_dump(exclude_unset=True)
    # Migrated DBs carry no FK on the ALTER-added persona_lock_id column, so
    # the API is the only guard against locking to a nonexistent persona.
    if update_data.get("persona_lock_id") is not None and not await get_user_persona(update_data["persona_lock_id"]):
        raise HTTPException(status_code=400, detail="Persona not found")
    result = await update_conversation(cid, update_data)
    return result


@router.post("/api/conversations/{cid}/summarize")
async def api_summarize_conversation(
    cid: str,
    data: SummarizeRequest,
    request: Request,
    conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Stream a narrative summary of the conversation history, excluding the last keep_count messages."""
    if data.keep_count not in (2, 4, 6, 8):
        raise HTTPException(status_code=400, detail="keep_count must be one of 2, 4, 6, 8")

    messages = await get_messages_with_branch_info(cid)
    history_slice = messages[: max(0, len(messages) - data.keep_count)]

    if not history_slice:
        raise HTTPException(status_code=400, detail="Not enough messages to summarize")

    settings = await get_settings()
    char_name = conv.get("character_name", "Character") or "Character"
    # Resolve the same effective persona the chat would use (conversation/character
    # lock overrides the global active persona) so a summary stays consistent.
    card, active_persona = await resolve_card_and_persona(conv, settings)
    system_prompt, char_persona, mes_example = await resolve_char_context(conv, settings, card=card)
    macros, user_description = persona_macros(settings, char_name, active_persona)

    abort_token = AbortToken()
    client = client_from_settings(settings, abort_token=abort_token)
    summarizer = ConversationSummarizer(client, settings)
    llm_messages = summarizer.build_messages(
        system_prompt,
        char_persona,
        conv.get("character_scenario", "") or "",
        mes_example,
        ("" if settings.get("prevent_prompt_overrides") else conv.get("post_history_instructions", "")),
        history_slice,
        macros,
        user_description,
        custom_instructions=data.custom_instructions,
    )

    async def _gen():
        try:
            async for delta in summarizer.stream(llm_messages, settings.get("model_name", "")):
                yield {"event": "token", "data": delta}
            yield {"event": "done", "data": ""}
        except Exception as e:
            logger.error("Summarize error: %s", e)
            yield {"event": "error", "data": "Summarize failed; see server logs"}

    return _CleanupStreamingResponse(
        _sse_stream(_gen(), request, abort_token=abort_token, cid=cid),
        media_type="text/event-stream",
    )


@router.post("/api/conversations/{cid}/compress")
async def api_compress_conversation(
    cid: str,
    data: CompressRequest,
    conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Create a new conversation seeded with a summary, then re-append the last keep_count messages."""
    if data.keep_count not in (2, 4, 6, 8):
        raise HTTPException(status_code=400, detail="keep_count must be one of 2, 4, 6, 8")
    if not data.summary.strip():
        raise HTTPException(status_code=400, detail="summary must not be empty")

    messages = await get_messages_with_branch_info(cid)
    tail = messages[max(0, len(messages) - data.keep_count) :]

    old_title = conv.get("title", "") or ""
    new_title = f"{old_title} (continued)" if old_title else "Continued"
    new_cid = await fork_conversation(conv, new_title)

    prev_id, _ = await add_message(new_cid, "assistant", data.summary.strip(), 0)
    await set_active_leaf(new_cid, prev_id)

    # Carry user uploads onto the fork; workflow attachments are regenerable and dropped.
    for i, msg in enumerate(tail):
        prev_id, _ = await add_message(
            new_cid,
            msg["role"],
            msg["content"],
            i + 1,
            parent_id=prev_id,
            attachments=user_attachment_payloads(msg),
        )
        await set_active_leaf(new_cid, prev_id)

    return {"new_conversation_id": new_cid}


async def _checkpoint_conversation(source_cid: str, new_title: str) -> ConversationRow | None:
    """Duplicate a conversation's active path into a fresh conversation.

    A "checkpoint" snapshots the *current* line of the story so the user can
    branch off it without disturbing the original. It carries the linear
    active-path messages (root→leaf), their user uploads, the director state
    (moods / progressive fields, so continuation behaves identically), and the
    per-turn conversation logs that drive the inspector.

    Two things are deliberately *not* carried, mirroring the "active path +
    user uploads only" contract the Compress History flow established:
      * non-active branches (alternate swipes / forks), and
      * workflow-generated attachments and workflow_state (regenerable; their
        bytes live in a budgeted cache and per-message state may point at
        attachment ids that would not exist on the copy).

    Returns the new conversation row, or None if *source_cid* is missing.
    """
    conv = await get_conversation(source_cid)
    if not conv:
        return None

    # Active path, root→leaf, with user_attachments already populated.
    messages = await get_messages(source_cid)

    new_cid = await fork_conversation(conv, new_title)

    # Re-insert the path linearly, remapping parent_id and recording old→new
    # message ids so the conversation_logs below can be re-pointed onto the copy.
    id_map: dict[int, int] = {}
    prev_id: int | None = None
    for msg in messages:
        new_id, _ = await add_message(
            new_cid,
            msg["role"],
            msg["content"],
            msg["turn_index"],
            parent_id=prev_id,
            attachments=user_attachment_payloads(msg),
            progressive_fields=msg.get("progressive_fields") or {},
        )
        id_map[msg["id"]] = new_id
        prev_id = new_id

    if prev_id is not None:
        await set_active_leaf(new_cid, prev_id)

    # Carry the director state verbatim so the first turn on the checkpoint
    # starts from the same moods / progressive fields as the original.
    director = await get_director_state(source_cid)
    await update_director_state(
        new_cid,
        director.get("active_moods", []),
        keywords=director.get("keywords", []),
        progressive_fields=director.get("progressive_fields", {}),
    )

    # Carry the per-turn inspector logs, re-pointing message_id onto the copied
    # rows. Logs tied to messages off the active path (other branches) or with
    # no message_id resolve to None in id_map and are skipped.
    for log in await get_conversation_logs(source_cid):
        src_msg_id = log.get("message_id")
        new_msg_id = id_map.get(src_msg_id) if src_msg_id is not None else None
        if new_msg_id is None:
            continue
        await add_conversation_log(
            new_cid,
            log["turn_index"],
            log.get("agent_raw_output") or "",
            log.get("tool_calls") or [],
            log.get("active_moods_after") or [],
            log.get("injection_block") or "",
            log.get("agent_latency_ms") or 0,
            progressive_fields=json.loads(log.get("progressive_fields_after") or "{}"),
            message_id=new_msg_id,
            reasoning_director=log.get("reasoning_director") or "",
            reasoning_writer=log.get("reasoning_writer") or "",
            reasoning_editor=log.get("reasoning_editor") or "",
            feedback=log.get("feedback") or {},
        )

    return await get_conversation(new_cid)


@router.post("/api/conversations/{cid}/checkpoint")
async def api_checkpoint_conversation(
    cid: str,
    data: CheckpointRequest,
    conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Duplicate the conversation's active path into a new 'checkpoint'
    conversation (SillyTavern-style). See :func:`_checkpoint_conversation` for
    exactly what is and isn't carried."""
    if data.title and data.title.strip():
        new_title = data.title.strip()
    else:
        base = conv.get("title") or conv.get("character_name") or "Conversation"
        new_title = f"{base} (checkpoint)"

    new_conv = await _checkpoint_conversation(cid, new_title)
    if new_conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return new_conv


@router.post("/api/conversations/{cid}/stop")
async def api_stop_generation(cid: str):
    """Abort the active LLM generation for this conversation, if any."""
    token = _active_aborts.get(cid)
    if token is not None:
        token.abort()
        logger.info("Stop Generation requested for conversation %s — abort signalled", scrub_log(cid))
    return {"ok": True}


@router.get("/api/conversations/{cid}/context-size")
async def api_get_context_size(cid: str, conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    settings = await get_settings()
    messages = await get_messages(cid)
    director = await get_director_state(cid) or {}
    director_frags = [f for f in await get_interactive_fragments() if f.get("enabled", True)]
    mood_frags = [f for f in await get_mood_fragments() if f.get("enabled", True)]
    lorebook_entries = await get_active_lorebook_entries()

    # Resolve the same effective persona generation would use (conversation/
    # character lock overrides the global active persona) so the size
    # breakdown matches the prompt that is actually sent.
    card, active_persona = await resolve_card_and_persona(conv, settings)
    macros, user_desc = persona_macros(settings, conv["character_name"], active_persona)

    # Resolve character context
    system_prompt, char_persona, mes_example = await resolve_char_context(conv, settings, card=card)

    # Measure each component individually
    sys_text = system_prompt or ""
    persona_text = macros.resolve_message(char_persona or "")
    scenario_text = macros.resolve_message(conv.get("character_scenario", "") or "")
    mes_text = macros.resolve_message(mes_example or "")
    post_text = macros.resolve_message(
        "" if settings.get("prevent_prompt_overrides") else (conv.get("post_history_instructions", "") or "")
    )
    resolved_user_desc = macros.resolve_message(user_desc)
    user_persona_text = f"## User: {macros.user}\n{resolved_user_desc}" if resolved_user_desc.strip() else ""
    msg_chars = sum(len(m.get("content", "") or "") for m in messages)

    # Director injection
    active_moods = director.get("active_moods", []) if director else []
    inj_block = prompt_builder.compute_style_injection_block(
        active_moods,
        active_moods,
        mood_frags,
        director_frags,
        agent_enabled(settings),
        {},
    )

    # Lorebook injection
    scan_depth = lorebook.LOREBOOK_SCAN_DEPTH
    recent_messages = messages[-scan_depth:] if len(messages) >= scan_depth else messages
    lorebook_block = lorebook.compute_lorebook_injection_block(recent_messages, lorebook_entries, macros)

    breakdown = {}
    for label, chars in [
        ("system_prompt", len(sys_text)),
        ("char_persona", len(persona_text)),
        ("scenario", len(scenario_text)),
        ("mes_example", len(mes_text)),
        ("user_persona", len(user_persona_text)),
        ("messages", msg_chars),
        ("post_history", len(post_text)),
        ("director_injection", len(inj_block)),
        ("lorebook", len(lorebook_block)),
    ]:
        breakdown[label] = {"chars": chars, "tokens_est": estimate_tokens(chars)}

    total_chars = sum(v["chars"] for v in breakdown.values())
    return {
        "total_chars": total_chars,
        "total_tokens_est": estimate_tokens(total_chars),
        "breakdown": breakdown,
        "message_count": len(messages),
    }


# Inspector ──


@router.get("/api/conversations/{cid}/director")
async def api_get_director_state(cid: str, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    return await get_director_state(cid)


@router.get("/api/conversations/{cid}/logs")
async def api_get_logs(cid: str, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    return await get_conversation_logs(cid)


@router.get("/api/conversations/{cid}/messages/{msg_id}/director-log")
async def api_get_message_director_log(
    cid: str,
    msg_id: int,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    msg = await get_message_by_id(msg_id)
    if not msg or msg.get("conversation_id") != cid:
        raise HTTPException(status_code=404, detail="Message not found")
    direction_notes = [direction_note_projection(r) for r in await get_direction_notes_for_message(msg_id)]
    log = await get_director_log_for_message(msg_id)
    if not log:
        return {
            "active_moods": [],
            "tool_calls": [],
            "injection_block": "",
            "agent_latency_ms": 0,
            "reasoning_director": "",
            "reasoning_writer": "",
            "reasoning_editor": "",
            "feedback": {},
            "direction_notes": direction_notes,
        }
    return {
        "active_moods": log.get("active_moods_after", []),
        "tool_calls": log.get("tool_calls", []),
        "injection_block": log.get("injection_block", ""),
        "agent_latency_ms": log.get("agent_latency_ms", 0),
        "reasoning_director": log.get("reasoning_director") or "",
        "reasoning_writer": log.get("reasoning_writer") or "",
        "reasoning_editor": log.get("reasoning_editor") or "",
        "feedback": log.get("feedback", {}) or {},
        "direction_notes": direction_notes,
    }


@router.get("/api/conversations/{cid}/direction-notes")
async def api_list_direction_notes(cid: str, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    messages = await get_messages(cid)
    by_id = {m["id"]: m for m in messages}
    rows = await get_direction_notes_for_path(cid, list(by_id))
    return [
        {
            "id": r["id"],
            **direction_note_projection(r),
            "message_id": r["message_id"],
            "turn_index": by_id[r["message_id"]]["turn_index"],
        }
        for r in rows
    ]


@router.post("/api/conversations/{cid}/direction-notes")
async def api_create_direction_note(cid: str, data: DirectionNoteCreate):
    content = data.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Note content is empty")
    msg = await get_message_by_id(data.message_id)
    if not msg or msg.get("conversation_id") != cid:
        raise HTTPException(status_code=404, detail="Message not found")
    ids = await create_direction_notes(
        cid,
        data.message_id,
        [
            {
                "interactive_fragment_id": _USER_NOTE_FRAGMENT_ID,
                "interactive_fragment_label": data.label.strip() or "Note",
                "content": content,
            }
        ],
    )
    return {"id": ids[0]}


@router.put("/api/conversations/{cid}/direction-notes/{fid}")
async def api_update_direction_note(cid: str, fid: int, data: DirectionNoteUpdate):
    updated = await update_direction_note(fid, data.content)
    if not updated:
        raise HTTPException(status_code=404, detail="Note not found")
    return updated


@router.delete("/api/conversations/{cid}/direction-notes/{fid}")
async def api_delete_direction_note(cid: str, fid: int):
    if not await delete_direction_note(fid):
        raise HTTPException(status_code=404, detail="Note not found")
    return {"ok": True}
