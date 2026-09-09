"""Message, branch, and streaming generation routes."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from ...core.macros import Macros, resolve_inline
from ...database import (
    clear_writer_draft,
    delete_message_with_descendants,
    get_changesets_for_messages,
    get_character_card,
    get_group_members,
    get_message_by_id,
    get_message_delete_preview,
    get_messages,
    get_messages_with_branch_info,
    get_settings,
    get_speaker_names,
    get_user_persona,
    get_worlds,
    mark_changesets_stale_for_messages,
    mark_orphaned_changesets_stale,
    reroll_unfrozen_greetings,
    set_workflow_message_state,
    switch_to_branch,
    update_message_content,
)
from ...database.models import ConversationRow
from ...features import autocomplete
from ...features.prose_rewriter import (
    ProseRewriteConfig,
    resolve_config,
    rewrite_events,
)
from ...inference import AbortToken, local_ml
from ...pipeline import (
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_speak,
    handle_super_regenerate,
    handle_turn,
)
from ...pipeline.predicates import resolve_persona_id
from ..deps import (
    _conversation_stream_lock,
    _pipeline_sse_response,
    require_conversation,
    stream_idle_lock,
)
from ..schemas import (
    AutocompleteInput,
    EditMessage,
    MagicRewriteMsg,
    RegenerateMsg,
    SendMessage,
    SpeakRequest,
)

router = APIRouter()


def _retained_draft(message: Mapping[str, Any]) -> str | None:
    """The row's retained pre-editor Writer draft, when it carries real text.

    Blank counts as absent: a draft of ``""`` is not a source, and letting it
    through would have the client promise a rewrite of text that is not there.
    """
    draft = message.get("writer_draft")
    return draft if isinstance(draft, str) and draft.strip() else None


def _prose_rewrite_source(message: Mapping[str, Any]) -> str | None:
    """Return the text an on-demand rewrite should use."""
    draft = _retained_draft(message)
    if draft is not None:
        return draft
    content = message.get("content") or ""
    return content if content.strip() else None


def _row_for_client(message: Mapping[str, Any]) -> dict:
    """Shape one message row for the API."""
    row = dict(message)
    row["has_writer_draft"] = _retained_draft(row) is not None
    row.pop("writer_draft", None)
    return row


async def _message_rows_for_client(messages: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Shape the active message path for the API."""
    ids = [int(m["id"]) for m in messages if m.get("id") is not None and m.get("role") == "assistant"]
    rows = await get_changesets_for_messages(ids)
    if not rows:
        return [_row_for_client(m) for m in messages]
    world_names = {w["id"]: w.get("name", "") for w in await get_worlds()}
    by_message: dict[int, list[dict]] = {}
    for row in rows:
        changeset = {**row, "world_name": world_names.get(row["world_id"], "")}
        by_message.setdefault(int(row["source_assistant_message_id"] or 0), []).append(changeset)
    out: list[dict] = []
    for m in messages:
        found = by_message.get(int(m["id"])) if m.get("id") is not None else None
        row = _row_for_client(m)
        if found:
            row["world_changesets"] = found
        out.append(row)
    return out


@router.get("/api/conversations/{cid}/messages")
async def api_get_messages(cid: str, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    # Greetings with inline macros re-roll freely on every fetch until the
    # first user message freezes them. The try-lock (never queued) makes the
    # re-roll mutually exclusive with a whole pipeline stream — skipped when
    # one is running — so a fetch can never commit a re-roll between the
    # stream's history read and its freeze, and the frozen bytes are always
    # the ones the model saw.
    async with stream_idle_lock(cid) as idle:
        if idle:
            await reroll_unfrozen_greetings(cid)
        return await _message_rows_for_client(await get_messages_with_branch_info(cid))


@router.get("/api/conversations/{cid}/messages/{msg_id}/delete-preview")
async def api_message_delete_preview(
    cid: str,
    msg_id: int,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    preview = await get_message_delete_preview(cid, msg_id)
    if preview is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return preview


@router.post("/api/conversations/{cid}/messages/{msg_id}/edit")
async def api_edit_message(
    cid: str,
    msg_id: int,
    data: EditMessage,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    # Serialize against an in-flight streaming pipeline on this cid: the
    # pipeline reads message content into the LLM prefix early and persists
    # the assistant reply late, so a mid-stream edit would make the on-disk
    # user message disagree with the prefix that produced the reply.
    async with _conversation_stream_lock(cid):
        original = await get_message_by_id(msg_id)
        if not original or original["conversation_id"] != cid:
            raise HTTPException(status_code=404, detail="Message not found")

        # Plain edits bypass the pipeline's persist boundary, so inline macros
        # ({{roll}}/{{random}}) typed into an edit fire once here.
        await update_message_content(msg_id, resolve_inline(data.content))
        # Editing an unfrozen greeting drops its stashed template — otherwise
        # the next fetch would re-roll from it and clobber the manual edit.
        if original["role"] == "assistant" and original["parent_id"] is None:
            await set_workflow_message_state(msg_id, "macros", None)
        # Same clobber, one surface over: the retained Writer draft describes
        # the text this edit just replaced, and the on-demand prose rewriter
        # prefers it over the saved content — so a rewrite after an edit would
        # quietly restore the pre-edit prose. Dropping it makes the rewriter
        # fall back to what the user actually wrote, which is what they mean by
        # "rewrite this message".
        if original["role"] == "assistant":
            await clear_writer_draft(msg_id)
        # An unreviewed world-change proposal was derived from this exact text.
        # Editing either source message invalidates that evidence, so the
        # proposal goes stale and must be re-evaluated rather than applied.
        await mark_changesets_stale_for_messages([msg_id])
        return {"ok": True}


@router.post("/api/conversations/{cid}/messages/{msg_id}/fork-edit")
async def api_fork_edit_message(
    cid: str,
    msg_id: int,
    data: EditMessage,
    request: Request,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Fork at a user message: persist an edited sibling and stream a fresh reply."""
    return _pipeline_sse_response(
        lambda tok: handle_fork_edit(
            cid,
            msg_id,
            data.content,
            abort_token=tok,
            speaker_member_id=data.speaker_member_id,
        ),
        request,
        cid,
    )


@router.delete("/api/conversations/{cid}/messages/{msg_id}")
async def api_delete_message(cid: str, msg_id: int, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    """Delete a message and all its descendants. Returns updated message list."""
    # Serialize against an in-flight streaming pipeline on this cid: ON
    # DELETE CASCADE on messages.parent_id would otherwise wipe the
    # in-flight assistant row mid-INSERT (IntegrityError) or right after
    # commit (silent disappearance).
    async with _conversation_stream_lock(cid):
        if not await delete_message_with_descendants(cid, msg_id):
            raise HTTPException(status_code=404, detail="Message not found")
        # The cascade has already NULLed every changeset pointer into the deleted
        # subtree, so orphanhood is what identifies the affected proposals — an
        # id list read before the delete would match nothing after it. Applied
        # history survives the same cascade with its denormalised labels intact.
        await mark_orphaned_changesets_stale()
        return await _message_rows_for_client(await get_messages_with_branch_info(cid))


@router.post("/api/conversations/{cid}/messages/{msg_id}/switch-branch")
async def api_switch_branch(cid: str, msg_id: int, _conv: ConversationRow = Depends(require_conversation)):  # noqa: B008
    """Switch to the branch containing msg_id (sets active leaf to deepest descendant)."""
    # Serialize against an in-flight streaming pipeline on this cid: the
    # pipeline's terminal set_active_leaf would otherwise overwrite the
    # branch the user just selected.
    async with _conversation_stream_lock(cid):
        success = await switch_to_branch(cid, msg_id)
        if not success:
            raise HTTPException(status_code=404, detail="Message not found")
        # Switching branches alters nothing about a proposal: a World has one
        # canonical timeline, and an accepted change stays canon even if its
        # source branch is later abandoned.
        return await _message_rows_for_client(await get_messages_with_branch_info(cid))


@router.post("/api/conversations/{cid}/messages/{msg_id}/regenerate")
async def api_regenerate_msg(
    cid: str,
    msg_id: int,
    request: Request,
    data: RegenerateMsg | None = None,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Regenerate a specific assistant message as a new sibling branch."""
    return _pipeline_sse_response(lambda tok: handle_regenerate(cid, msg_id, abort_token=tok), request, cid)


@router.post("/api/conversations/{cid}/messages/{msg_id}/super_regenerate")
async def api_super_regenerate_msg(
    cid: str,
    msg_id: int,
    request: Request,
    data: RegenerateMsg | None = None,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Super-regenerate: keeps prior response as context, asks model for a different direction."""
    return _pipeline_sse_response(lambda tok: handle_super_regenerate(cid, msg_id, abort_token=tok), request, cid)


@router.post("/api/conversations/{cid}/messages/{msg_id}/magic_rewrite")
async def api_magic_rewrite_msg(
    cid: str,
    msg_id: int,
    request: Request,
    data: MagicRewriteMsg,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Magic rewrite: runs the full pipeline as a new sibling steered by a user-supplied direction."""
    return _pipeline_sse_response(lambda tok: handle_magic_rewrite(cid, msg_id, data.direction, abort_token=tok), request, cid)


async def _stream_prose_rewrite_message(
    cid: str,
    msg_id: int,
    config: ProseRewriteConfig,
    abort_token: AbortToken,
) -> AsyncIterator[dict]:
    """Stream an assistant row's Writer draft — or its saved text — through the local rewriter.

    The shared prose step provides whole-draft snapshots in visible document
    order. Unlike the in-turn caller, this stream persists only after its
    final ``rewritten`` event, so a disconnected or failed request leaves the
    saved message byte-identical. This generator begins only after the SSE
    layer acquires the conversation lock, so loading the row here prevents a
    pre-stream edit from being overwritten with stale content — which is also
    why the source is resolved here and not carried in from the route.
    """
    message = await get_message_by_id(msg_id)
    if not message or message["conversation_id"] != cid or message["role"] != "assistant":
        yield {"event": "error", "data": "Message changed or was deleted before the prose rewrite started"}
        return
    source = _prose_rewrite_source(message)
    if source is None:
        yield {"event": "error", "data": "This message has no text to rewrite"}
        return
    current_content = message["content"] or ""
    rewritten = source
    warning = ""
    events = rewrite_events(source, config)
    try:
        while True:
            next_event = asyncio.create_task(anext(events))
            abort_wait = asyncio.create_task(abort_token.wait())
            done, _pending = await asyncio.wait({next_event, abort_wait}, return_when=asyncio.FIRST_COMPLETED)
            if abort_wait in done:
                if not next_event.done():
                    next_event.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, Exception):
                    await next_event
                yield {
                    "event": "prose_rewrite_done",
                    "data": {
                        "message_id": msg_id,
                        "content": current_content,
                        "changed": False,
                        "warning": "",
                        "aborted": True,
                    },
                }
                return

            abort_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await abort_wait
            try:
                event = next_event.result()
            except StopAsyncIteration:
                break
            if event["type"] == "draft_update":
                yield {"event": "prose_rewrite_update", "data": {"message_id": msg_id, "draft": event["draft"]}}
            elif event["type"] == "rewritten":
                rewritten = event["draft"]
            elif event["type"] == "warning":
                warning = event["reason"]
    finally:
        await events.aclose()

    changed = not warning and rewritten != current_content
    if changed:
        await update_message_content(msg_id, rewritten)
        await mark_changesets_stale_for_messages([msg_id])
    yield {
        "event": "prose_rewrite_done",
        "data": {
            "message_id": msg_id,
            "content": rewritten if not warning else current_content,
            "changed": changed,
            "warning": warning,
        },
    }


@router.post("/api/conversations/{cid}/messages/{msg_id}/prose-rewrite")
async def api_prose_rewrite_message(
    cid: str,
    msg_id: int,
    request: Request,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Stream a prose rewrite for one saved assistant message."""
    message = await get_message_by_id(msg_id)
    if not message or message["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found")
    if message["role"] != "assistant":
        raise HTTPException(status_code=400, detail="Only assistant messages can be rewritten")
    if _prose_rewrite_source(message) is None:
        raise HTTPException(status_code=409, detail="This message has no text to rewrite")

    config = resolve_config(await get_settings())
    if config is None:
        raise HTTPException(
            status_code=503,
            detail="Prose rewriter unavailable: enable it and download a model in Settings → Local ML",
        )
    return _pipeline_sse_response(
        lambda tok: _stream_prose_rewrite_message(cid, msg_id, config, tok),
        request,
        cid,
    )


@router.post("/api/conversations/{cid}/send")
async def api_send_message(
    cid: str,
    data: SendMessage,
    request: Request,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    attachments = [a.model_dump() for a in data.attachments]
    return _pipeline_sse_response(
        lambda tok: handle_turn(
            cid,
            data.content,
            attachments=attachments,
            abort_token=tok,
            speaker_member_id=data.speaker_member_id,
        ),
        request,
        cid,
    )


@router.post("/api/conversations/{cid}/speak")
async def api_group_speak(
    cid: str,
    data: SpeakRequest,
    request: Request,
    conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    if conv.get("kind", "solo") != "group":
        raise HTTPException(status_code=409, detail="Conversation is not a group")
    return _pipeline_sse_response(lambda tok: handle_speak(cid, data.speaker_member_id, abort_token=tok), request, cid)


@router.post("/api/conversations/{cid}/continue")
async def api_continue_from_user(
    cid: str,
    request: Request,
    data: RegenerateMsg | None = None,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Generate an assistant response for the current user turn without creating a new message."""
    messages = await get_messages(cid)
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="Last message is not a user message")
    user_content = messages[-1]["content"]
    return _pipeline_sse_response(
        lambda tok: handle_turn(
            cid,
            user_content,
            skip_user_persist=True,
            abort_token=tok,
            speaker_member_id=data.speaker_member_id if data else None,
        ),
        request,
        cid,
    )


@router.post("/api/conversations/{cid}/autocomplete")
async def api_autocomplete(
    cid: str,
    data: AutocompleteInput,
    conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Predict a short continuation of the user's in-progress draft (CPU, in-process).

    Opt-in: 503 unless the llama-cpp-python extra and the GGUF are present. Uses a
    trimmed context (char/persona names + last few messages), NOT the Director pipeline.
    """
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

    # A group is a scene, not a character: {{char}} is its title, the "who am I
    # talking to" summary is the roster, and each replayed line is labelled with
    # the member who actually said it — the same three substitutions the pipeline
    # makes. Without them the typeahead completes against one nameless character.
    #
    # Read straight off the roster rather than through `resolve_cast`: this route
    # fires on a typing debounce, and all it needs are names — not the card behind
    # each one. `{{cast}}` stays empty in a solo chat, exactly as `_prepare_turn`
    # leaves it, so a draft resolves here the way it will in the turn itself.
    speaker_names: dict[str, str] = {}
    cast_names = ""
    if conv.get("kind", "solo") == "group":
        # Names from the active roster (what the scene is), labels from all of it
        # (so a reply by a since-removed member is still attributed).
        cast_names = ", ".join(m["display_name"] for m in await get_group_members(cid))
        char_name = conv.get("title") or conv.get("character_name") or "Character"
        summary_source = f"Scene cast: {cast_names}"
        speaker_names = await get_speaker_names(cid)
    else:
        char_name = conv.get("character_name") or (card or {}).get("name") or "Character"
        summary_source = (card or {}).get("description") or ""

    macros = Macros(user=user_name, char=char_name, cast=cast_names)
    messages = await get_messages(cid)
    recent = [
        {
            "role": m["role"],
            "content": macros.resolve_prompt(m["content"] or ""),
            "name": speaker_names.get(str(m.get("speaker_member_id")), "") if m.get("speaker_member_id") else "",
        }
        for m in messages[-4:]
    ]
    summary = macros.resolve_prompt(summary_source)
    prompt = autocomplete.build_prompt(char_name, user_name, summary, recent, macros.resolve_prompt(data.draft))

    completion = await autocomplete.complete(prompt)
    return {"completion": completion}
