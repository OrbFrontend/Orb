"""Conversation lifecycle, summarize/compress/checkpoint, context-size,
stop, and Inspector (director / logs / director-log) routes."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core import (
    estimate_tokens,
    has_inline_macros,
    resolve_inline,
    scrub_log,
)
from ...database import (
    activate_character_linked_worlds,
    add_conversation_log,
    add_message,
    card_embedded_fragments,
    convert_to_group,
    create_conversation,
    create_direction_notes,
    create_group_conversation,
    delete_conversation,
    delete_direction_note,
    direction_note_projection,
    disable_character_linked_worlds,
    fork_conversation,
    get_active_lorebook_entries,
    get_character_card,
    get_conversation,
    get_conversation_logs,
    get_direction_notes_for_message,
    get_direction_notes_for_path,
    get_director_log_for_message,
    get_director_state,
    get_group_members,
    get_interactive_fragments,
    get_message_by_id,
    get_messages,
    get_messages_with_branch_info,
    get_mood_fragments,
    get_settings,
    get_user_persona,
    insert_alternate_greeting_swipes,
    list_conversations,
    mark_orphaned_changesets_stale,
    resolve_char_context,
    set_active_leaf,
    set_workflow_message_state,
    sync_group_members,
    touch_conversation,
    update_conversation,
    update_direction_note,
    update_director_state,
    user_attachment_payloads,
)
from ...database.models import ConversationRow
from ...features import lorebook
from ...features.summarization import ConversationSummarizer
from ...inference import AbortToken, client_from_settings, group_context, prompt_builder
from ...pipeline import (
    agent_enabled,
    conversation_macro_seed,
    persona_macros,
    resolve_card_and_persona,
)
from ...pipeline.cast import resolve_cast
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
    GroupRosterUpdate,
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

    if data.kind == "group":
        if not data.members:
            raise HTTPException(status_code=400, detail="A group needs at least one member")
        member_specs = []
        for spec in data.members:
            payload = spec.model_dump()
            if spec.character_card_id:
                card = await get_character_card(spec.character_card_id)
                if not card:
                    raise HTTPException(status_code=404, detail=f"Character card not found: {spec.character_card_id}")
                if not payload.get("display_name"):
                    payload["display_name"] = card.get("name", "")
            member_specs.append(payload)
        try:
            return await create_group_conversation(
                cid,
                data.title,
                member_specs,
                scenario=data.character_scenario,
                post_history_instructions=data.post_history_instructions,
                turn_mode=data.group_turn_mode,
                max_speakers=data.group_max_speakers,
                context_mode=data.group_context_mode,
                greeting=resolve_inline(data.first_mes),
                greeting_speaker_key=data.opening_speaker_key,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    # If there's a first message, auto-add it as the first assistant turn.
    # Content is stored macro-resolved; the raw template rides the per-message
    # "macros" slot so the greeting can re-roll freely until the first user
    # message freezes it (see reroll_unfrozen_greetings).
    if first_mes.strip():
        raw_greeting = first_mes.strip()
        msg_id, _ = await add_message(cid, "assistant", resolve_inline(raw_greeting), 0, attachments=None)
        await set_active_leaf(cid, msg_id)
        if has_inline_macros(raw_greeting):
            await set_workflow_message_state(msg_id, "macros", {"template": raw_greeting})

        # If we have a character card with alternate greetings, create swipe versions
        if card_id:
            card = await get_character_card(card_id)
            if card:
                alternate_greetings = card.get("alternate_greetings", [])
                count = await insert_alternate_greeting_swipes(cid, alternate_greetings)
                if count:
                    logger.info(f"Created {count} alternate greeting swipes for conversation {cid}")

    return conv


@router.get("/api/conversations/{cid}/members")
async def api_get_group_members(
    cid: str,
    include_inactive: bool = False,
    conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    if conv.get("kind", "solo") != "group":
        raise HTTPException(status_code=409, detail="Conversation is not a group")
    return await get_group_members(cid, include_inactive=include_inactive)


@router.put("/api/conversations/{cid}/members")
async def api_sync_group_members(
    cid: str,
    data: GroupRosterUpdate,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    if not data.members:
        raise HTTPException(status_code=400, detail="A group needs at least one active member")
    existing = {member["id"]: member for member in await get_group_members(cid, include_inactive=True)}
    member_specs: list[dict] = []
    for spec in data.members:
        payload = spec.model_dump()
        if spec.character_card_id:
            card = await get_character_card(spec.character_card_id)
            old = existing.get(spec.id or "")
            # A deleted card's stable dangling id is retained on its existing
            # member so re-import can relink it. New/reassigned missing ids are
            # invalid roster input.
            missing_allowed = old is not None and old.get("character_card_id") == spec.character_card_id
            if card is None and not missing_allowed:
                raise HTTPException(status_code=404, detail=f"Character card not found: {spec.character_card_id}")
            if card and not payload.get("display_name"):
                payload["display_name"] = card.get("name", "")
        member_specs.append(payload)
    try:
        return await sync_group_members(cid, member_specs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/conversations/{cid}/convert-to-group")
async def api_convert_to_group(cid: str, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    try:
        member = await convert_to_group(cid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"conversation": await get_conversation(cid), "member": member}


@router.post("/api/conversations/{cid}/activate")
async def api_activate_conversation(cid: str, conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    """Activate every linked World in the selected cast while preserving floating Worlds."""
    await disable_character_linked_worlds()
    card_ids: list[str] = []
    if conv.get("kind", "solo") == "group":
        card_ids = [str(card_id) for m in await get_group_members(cid) if (card_id := m.get("character_card_id"))]
    elif card_id := conv.get("character_card_id"):
        card_ids = [card_id]
    enabled = await activate_character_linked_worlds(card_ids)
    return {"ok": True, "world_ids": enabled}


@router.delete("/api/conversations/{cid}")
async def api_delete_conversation(cid: str):
    if not await delete_conversation(cid):
        raise HTTPException(status_code=404, detail="Conversation not found")
    # The cascade NULLed the source pointers of every changeset raised in this
    # chat. Unreviewed proposals lose the evidence they were derived from and go
    # stale; applied ones stay canon, carrying their denormalised labels.
    await mark_orphaned_changesets_stale()
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
    macros, user_description = persona_macros(settings, char_name, active_persona, seed=conversation_macro_seed(conv))
    # The one place the group context mode deliberately does *not* apply.
    # Compression is scene-wide narration, so it always reads the public-cast
    # projection: paying for every dossier — or swapping in one arbitrary card
    # the summary is not written from — buys nothing and inflates the single
    # longest call in the app.
    summary_cast = (await resolve_cast(conv))._replace(context_mode="private")
    speaker_names: dict[str, str] = {}
    if summary_cast.grouped:
        macros = macros._replace(cast=", ".join(member.name for member in summary_cast.members))
        speaker_names = {member["id"]: member["display_name"] for member in await get_group_members(cid, include_inactive=True)}

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
        cast=summary_cast,
        speaker_names=speaker_names,
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

    member_map: dict[str, str] = {}
    if conv.get("kind", "solo") == "group":
        old_members, new_members = (
            await get_group_members(cid, include_inactive=True),
            await get_group_members(new_cid, include_inactive=True),
        )
        new_by_key = {m["speaker_key"]: m["id"] for m in new_members}
        member_map = {m["id"]: new_by_key[m["speaker_key"]] for m in old_members if m["speaker_key"] in new_by_key}

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
            speaker_member_id=member_map.get(str(msg.get("speaker_member_id"))) if msg.get("speaker_member_id") else None,
            beat_id=msg.get("beat_id"),
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
    member_map: dict[str, str] = {}
    if conv.get("kind", "solo") == "group":
        old_members, new_members = (
            await get_group_members(source_cid, include_inactive=True),
            await get_group_members(new_cid, include_inactive=True),
        )
        new_by_key = {m["speaker_key"]: m["id"] for m in new_members}
        member_map = {m["id"]: new_by_key[m["speaker_key"]] for m in old_members if m["speaker_key"] in new_by_key}

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
            speaker_member_id=member_map.get(str(msg.get("speaker_member_id"))) if msg.get("speaker_member_id") else None,
            beat_id=msg.get("beat_id"),
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
        macro_choices=director.get("macro_choices", {}),
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
            log.get("tool_calls") or [],
            log.get("active_moods_after") or [],
            log.get("injection_block") or "",
            log.get("agent_latency_ms") or 0,
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
    conversation. See :func:`_checkpoint_conversation` for
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

    # Resolve the same effective persona generation would use (conversation/
    # character lock overrides the global active persona) so the size
    # breakdown matches the prompt that is actually sent.
    card, active_persona = await resolve_card_and_persona(conv, settings)
    turn_cast = await resolve_cast(conv)
    # Same card-fragment merge as _load_pipeline_context (globals win on id collision).
    card_moods, card_interactive = card_embedded_fragments(card)
    if turn_cast.grouped:
        seen_cards: set[str] = set()
        for member in turn_cast.members:
            if not member.card_id or member.card_id in seen_cards:
                continue
            seen_cards.add(member.card_id)
            member_card = await get_character_card(member.card_id)
            moods, interactive = card_embedded_fragments(member_card)
            card_moods.extend(moods)
            card_interactive.extend(interactive)
    director_frags = [f for f in await get_interactive_fragments() if f.get("enabled", True)]
    director_ids = {fragment["id"] for fragment in director_frags}
    for fragment in card_interactive:
        if fragment["id"] not in director_ids:
            director_frags.append(fragment)
            director_ids.add(fragment["id"])
    mood_frags = [f for f in await get_mood_fragments() if f.get("enabled", True)]
    mood_ids = {fragment["id"] for fragment in mood_frags}
    for fragment in card_moods:
        if fragment["id"] not in mood_ids:
            mood_frags.append(fragment)
            mood_ids.add(fragment["id"])
    lorebook_entries = await get_active_lorebook_entries()
    macro_char = conv.get("title", "") if turn_cast.grouped else conv["character_name"]
    macros, user_desc = persona_macros(settings, macro_char, active_persona, seed=conversation_macro_seed(conv))
    if turn_cast.grouped:
        macros = macros._replace(cast=", ".join(member.name for member in turn_cast.members))

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
    # The group breakdown is a *maximum* call, not a sum, and its shape follows
    # the context mode: the shared body once, plus the largest single speaker's
    # share of it. Rendered through the same projection the prompt uses, so the
    # estimate cannot drift from what is actually sent.
    group_components: list[tuple[str, str]] = []
    if turn_cast.grouped:
        persona_text = ""
        mes_text = ""
        post_text = ""
        group_components = group_context.context_size_components(
            turn_cast,
            macros,
            prevent_prompt_overrides=bool(settings.get("prevent_prompt_overrides")),
        )
    resolved_user_desc = macros.resolve_message(user_desc)
    user_persona_text = f"## User: {macros.user}\n{resolved_user_desc}" if resolved_user_desc.strip() else ""
    msg_chars = sum(len(m.get("content", "") or "") for m in messages)
    if turn_cast.grouped:
        names = {member["id"]: member["display_name"] for member in await get_group_members(cid, include_inactive=True)}
        for message in messages:
            if message.get("role") != "assistant":
                continue
            speaker_id = message.get("speaker_member_id")
            label = names.get(speaker_id, "Unknown") if speaker_id is not None else "Summary"
            msg_chars += len(f"{label}: ")

    # Director injection — fragment {{random}} resolves against a throwaway
    # copy of the stored choice map so the estimate matches the prompt bytes a
    # real turn would inject, without recording new picks.
    active_moods = director.get("active_moods", []) if director else []
    est_choices = dict(director.get("macro_choices", {}) if director else {})
    est_mood_frags = prompt_builder.resolve_mood_fragment_randoms(mood_frags, active_moods, est_choices)
    inj_block = prompt_builder.compute_style_injection_block(
        active_moods,
        active_moods,
        est_mood_frags,
        director_frags,
        agent_enabled(settings),
        {},
    )

    # Lorebook: trailing keyword-scanned block + constant prefix section + @Depth tail
    scan_depth = lorebook.LOREBOOK_SCAN_DEPTH
    recent_messages = messages[-scan_depth:] if len(messages) >= scan_depth else messages
    lorebook_block = lorebook.compute_lorebook_injection_block(recent_messages, lorebook_entries, macros)
    constant_lorebook_block = lorebook.compute_constant_lorebook_block(lorebook_entries, macros)
    depth_lorebook_block = lorebook.compute_depth_lorebook_block(lorebook_entries, macros)

    components = [
        ("system_prompt", len(sys_text)),
        ("char_persona", len(persona_text)),
        ("scenario", len(scenario_text)),
        ("mes_example", len(mes_text)),
        ("user_persona", len(user_persona_text)),
        ("messages", msg_chars),
        ("post_history", len(post_text)),
        ("director_injection", len(inj_block)),
        ("lorebook", len(lorebook_block)),
        ("lorebook_constant", len(constant_lorebook_block)),
        ("lorebook_depth", len(depth_lorebook_block)),
    ]
    if turn_cast.grouped:
        components[2:2] = [(key, len(text)) for key, text in group_components]
    breakdown = {}
    for label, chars in components:
        breakdown[label] = {"chars": chars, "tokens_est": estimate_tokens(chars)}

    total_chars = sum(v["chars"] for v in breakdown.values())
    return {
        "total_chars": total_chars,
        "total_tokens_est": estimate_tokens(total_chars),
        "breakdown": breakdown,
        "message_count": len(messages),
        "estimate_kind": "maximum" if turn_cast.grouped else "single_call",
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
