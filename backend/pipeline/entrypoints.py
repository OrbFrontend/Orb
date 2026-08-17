"""
entrypoints.py — The five public turn handlers and the shared turn driver.

Wires together context loading, the pass orchestrator, and persistence:

* :func:`handle_turn` / :func:`handle_fork_edit` / :func:`handle_regenerate` /
  :func:`handle_super_regenerate` / :func:`handle_magic_rewrite` -- arrange
  history and turn indices, persist the user row when one is needed, then
  delegate to :func:`_generate_reply` (setup -> pipeline -> persist).

``_resolve_target_and_parent`` and ``_prepare_regen_context`` are shared helpers
for the regenerate family: load the target message, rebuild branch history, and
reset the director to the branch baseline. ``_regenerate_with_steering`` backs
both super-regenerate and magic-rewrite, which differ only in the steering
message they inject.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from .. import database as db
from ..core import resolve_inline
from ..inference import AbortToken, prefix_is_speaker_scoped
from .cast import parse_speaking_plan, round_robin_member
from .config import _resolve_pipeline_config, _split_interactive_fragments
from .context import (
    PipelineContext,
    _build_prefixes,
    _load_pipeline_context,
    _prepare_turn,
    _TurnSetup,
)
from .failures import describe_failure
from .orchestrator import _run_pipeline
from .passes.director import director_stage, progressive
from .passes.editor.editor import AUDIT_BASELINE_WINDOW
from .persistence import _consume_pipeline, _conversation_log_writer
from .state import TurnState

logger = logging.getLogger(__name__)


def _group_pin_error(ctx: PipelineContext, pinned_speaker_id: str | None) -> str | None:
    if not ctx.cast.grouped:
        return None
    eligible = [member for member in ctx.group_members if not member.get("muted")]
    if pinned_speaker_id and not any(member["id"] == pinned_speaker_id for member in eligible):
        return "Pinned speaker is not active and unmuted"
    if not pinned_speaker_id and ctx.conv.get("group_turn_mode") == "manual":
        return "Manual group turns require a pinned speaker"
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Shared turn driver + regenerate helpers
# ═══════════════════════════════════════════════════════════════════════════════


async def _run_turn_handler(
    conversation_id: str,
    abort_token: AbortToken | None,
    body: Callable[[PipelineContext], AsyncIterator[dict]],
    *,
    log_label: str,
) -> AsyncIterator[dict]:
    """Shared wrapper for the public turn handlers.

    Loads the pipeline context, guards the missing-conversation case, and
    converts any pipeline exception into the terminal SSE error event — one
    place defines the error wire contract for every handler. The payload is
    ``describe_failure``'s dict, so the provider's own sentence survives to the
    browser instead of being replaced by a constant (see failures.py).
    """
    try:
        ctx = await _load_pipeline_context(conversation_id, abort_token=abort_token)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return
        async for event in body(ctx):
            yield event
    except Exception as e:
        logger.exception("%s error", log_label)
        yield {"event": "error", "data": describe_failure(e)}


async def _load_direction_notes(ctx: PipelineContext, conversation_id: str, path: Sequence[Mapping[str, Any]]) -> None:
    """Seed ``ctx.director['direction_notes']`` with the active-branch notes.

    Reconstructed from the messages on *path*, so the set is branch-correct; each note
    carries its authoring fragment's label and the turn it was recorded on (mapped from
    the path). Always loaded (cheap, empty when no notes exist) -- whether the notes are
    injected into the prompt or shown to the recording step is decided by their own gates
    downstream, independent of one another.
    """
    rows = await db.get_direction_notes_for_path(conversation_id, [m["id"] for m in path])
    turn_by_message = {m["id"]: m.get("turn_index") for m in path}
    ctx.director["direction_notes"] = [
        {**db.direction_note_projection(r), "turn_index": turn_by_message.get(r["message_id"])} for r in rows
    ]


async def _resolve_target_and_parent(
    conversation_id: str, assistant_msg_id: int
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | str:
    """Load an assistant message and its parent message.

    Returns ``(target, user_msg)`` on success, or an error string if the
    message is missing, belongs to a different conversation, or is not an
    assistant message.
    """
    target = await db.get_message_by_id(assistant_msg_id)
    if not target or target["conversation_id"] != conversation_id or target["role"] != "assistant":
        return "Invalid target message"
    parent_id = target["parent_id"]
    parent = await db.get_message_by_id(parent_id) if parent_id else None
    if not parent:
        return "Parent message not found"
    return target, parent


async def _prepare_regen_context(
    ctx: PipelineContext,
    conversation_id: str,
    target: Mapping[str, Any],
    parent_msg: Mapping[str, Any],
) -> tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]:
    """Load history and attachments for a regeneration, and reset the director.

    Resets the director's active moods and progressive fields to the pre-turn
    baseline so the regenerated reply starts from the same state as the original.
    Returns ``(history, attachments)``.
    """
    if parent_msg.get("role") == "user":
        history_parent_id: int | None = parent_msg.get("parent_id")
    else:
        history_parent_id = parent_msg.get("id")
    history = await db.get_path_to_leaf(conversation_id, history_parent_id) if history_parent_id is not None else []
    moods_before = await db.get_moods_before_turn(conversation_id, target["turn_index"] - 1)
    ctx.director["active_moods"] = moods_before
    ctx.director["progressive_fields"] = progressive.branch_baseline(history)
    await _load_direction_notes(ctx, conversation_id, history)
    attachments = await db.get_user_attachments_for_message(parent_msg["id"]) if parent_msg.get("role") == "user" else []
    return history, attachments


async def _generate_reply(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    settings: Mapping[str, Any],
    last_user_message: str,
    lorebook_messages: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Sequence[Mapping[str, Any]],
    user_msg_id: int | None,
    asst_turn_index: int,
    log_turn_index: int,
    editor_audit_msgs: list[str] | None = None,
) -> AsyncIterator[dict]:
    """Run setup → pipeline → persist and stream all SSE events.

    The user message row must already be persisted before this is called.

    *user_message* is what the writer actually receives; it may differ from
    *last_user_message* (the steered paths send an OOC message as the writer
    input while *last_user_message* carries the original).
    """
    setup: _TurnSetup | None = None
    async for ev in _prepare_turn(
        ctx,
        conversation_id,
        history=history,
        settings=settings,
        last_user_message=last_user_message,
        lorebook_messages=lorebook_messages,
    ):
        if isinstance(ev, _TurnSetup):
            setup = ev
        else:
            yield ev
    assert setup is not None

    pipeline = _run_pipeline(
        ctx.client,
        settings,
        ctx.director,
        ctx.mood_fragments,
        ctx.interactive_fragments,
        user_message,
        attachments=attachments,
        phrase_bank=ctx.phrase_bank,
        lorebook=setup.lorebook,
        editor_audit_msgs=editor_audit_msgs,
        agent_client=ctx.agent_client,
        agent_prefix=setup.agent_prefix,
        macros=setup.macros,
        conversation_id=conversation_id,
        character_id=ctx.conv.get("character_card_id"),
        card=ctx.card,
        prefix=setup.prefix,
        enabled_tools=setup.merged_enabled_tools,
        turn_scratch=setup.turn_scratch,
        kv_tracker=setup.kv_tracker,
        schema_overrides=setup.schema_overrides,
        history=history,
        world_proposal=setup.world_proposal,
    )
    async for event in _consume_pipeline(
        pipeline,
        conversation_id,
        settings,
        user_msg_id,
        asst_turn_index,
        extra_on_result=_conversation_log_writer(conversation_id, log_turn_index),
    ):
        yield event


async def _generate_group_beat(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Sequence[Mapping[str, Any]],
    parent_message_id: int | None,
    first_turn_index: int,
    beat_id: str,
    pinned_speaker_id: str | None,
    append_user_to_history: bool = True,
    source_user_message_id: int | None = None,
) -> AsyncIterator[dict]:
    """Run one shared Director setup followed by zero or more speaker pipelines."""
    settings = ctx.settings
    rows = list(ctx.group_members)
    eligible = [m for m in rows if not m.get("muted")]
    if error := _group_pin_error(ctx, pinned_speaker_id):
        yield {"event": "error", "data": error}
        return

    setup: _TurnSetup | None = None
    lorebook_messages = [*history, *([{"role": "user", "content": user_message}] if user_message else [])]
    async for ev in _prepare_turn(
        ctx,
        conversation_id,
        history=history,
        settings=settings,
        last_user_message=user_message,
        lorebook_messages=lorebook_messages,
    ):
        if isinstance(ev, _TurnSetup):
            setup = ev
        else:
            yield ev
    assert setup is not None

    cfg = _resolve_pipeline_config(
        settings,
        setup.merged_enabled_tools,
        macros=setup.macros,
        client=ctx.client,
        agent_client=ctx.agent_client,
        agent_prefix=setup.agent_prefix,
        prefix=setup.prefix,
        phrase_bank=ctx.phrase_bank,
        schema_overrides=setup.schema_overrides,
    )
    writer_fragments, _, _ = _split_interactive_fragments(ctx.interactive_fragments)
    shared = TurnState(
        user_message=setup.macros.resolve_message(user_message),
        effective_msg=setup.macros.resolve_message(user_message),
        active_moods=ctx.director["active_moods"],
        macro_choices=dict(ctx.director.get("macro_choices") or {}),
    )
    async for ev in director_stage(
        cfg,
        shared,
        settings=settings,
        director=ctx.director,
        mood_fragments=ctx.mood_fragments,
        writer_fragments=writer_fragments,
        attachments=attachments,
        kv_tracker=setup.kv_tracker,
        lorebook=setup.lorebook,
        macros=setup.macros,
    ):
        yield ev
    if ctx.client.is_aborted:
        yield {"event": "done"}
        return

    plan_rows: list[tuple[Mapping[str, Any], str]]
    if pinned_speaker_id:
        pinned = next(m for m in eligible if m["id"] == pinned_speaker_id)
        plan_rows = [(pinned, "")]
    elif ctx.conv.get("group_turn_mode") == "round_robin":
        member = round_robin_member(rows, history)
        plan_rows = [(member, "")] if member else []
    else:
        parsed = parse_speaking_plan(shared.extra_fields.get("speaking_plan"), rows, int(ctx.conv["group_max_speakers"]))
        if parsed is None:
            member = round_robin_member(rows, history)
            plan_rows = [(member, "")] if member else []
        else:
            plan_rows = parsed

    cast_by_id = {member.member_id: member for member in ctx.cast.members}
    public_plan = [
        {
            "member_id": row["id"],
            "card_id": row.get("character_card_id"),
            "name": row["display_name"],
            "beat": beat,
        }
        for row, beat in plan_rows
    ]
    yield {"event": "speaking_plan", "data": {"beat_id": beat_id, "plan": public_plan}}

    grown_history = list(history)
    if append_user_to_history and parent_message_id is not None and user_message:
        grown_history.append(
            {
                "id": parent_message_id,
                "role": "user",
                "content": user_message,
                "turn_index": first_turn_index - 1,
                "parent_id": history[-1]["id"] if history else None,
                "speaker_member_id": None,
                "beat_id": beat_id,
            }
        )

    # Under Classic card swap the shared setup base is the *neutral* one — the
    # Director ran before a speaker was known — so even the first speaker has to
    # rebuild its prefix around its own card. Reusing the setup base there is a
    # correctness bug in that mode, not merely a cache miss.
    speaker_scoped = prefix_is_speaker_scoped(ctx.cast.context_mode)
    current_parent = parent_message_id
    for index, (row, speaker_beat) in enumerate(plan_rows):
        if ctx.client.is_aborted:
            break
        speaker = cast_by_id.get(str(row["id"]))
        if speaker is None:
            break
        turn_index = first_turn_index + index
        yield {
            "event": "speaker_start",
            "data": {
                "beat_id": beat_id,
                "member_id": speaker.member_id,
                "card_id": speaker.card_id,
                "name": speaker.name,
                "index": index,
                "total": len(plan_rows),
                "beat": speaker_beat,
            },
        }
        if index == 0:
            effective_message = user_message
            pass_attachments = attachments
            pipeline_history = history
        else:
            effective_message = ""
            pass_attachments = []
            pipeline_history = grown_history
        if index == 0 and not speaker_scoped:
            prefix, agent_prefix = setup.prefix, setup.agent_prefix
        else:
            prefix, agent_prefix = _build_prefixes(
                ctx,
                pipeline_history,
                extra_system_blocks=list(setup.extra_system_blocks),
                speaker=speaker,
            )
        card = await db.get_character_card(speaker.card_id) if speaker.card_id else None
        pipeline = _run_pipeline(
            ctx.client,
            settings,
            ctx.director,
            ctx.mood_fragments,
            ctx.interactive_fragments,
            effective_message,
            attachments=pass_attachments,
            phrase_bank=ctx.phrase_bank,
            lorebook=setup.lorebook,
            agent_client=ctx.agent_client,
            agent_prefix=agent_prefix,
            macros=setup.macros,
            conversation_id=conversation_id,
            character_id=speaker.card_id,
            card=card,
            prefix=prefix,
            enabled_tools=setup.merged_enabled_tools,
            turn_scratch=setup.turn_scratch,
            kv_tracker=setup.kv_tracker,
            schema_overrides=setup.schema_overrides,
            history=pipeline_history,
            world_proposal=setup.world_proposal if index == len(plan_rows) - 1 else None,
            speaker=speaker,
            speaker_beat=speaker_beat,
            context_mode=ctx.cast.context_mode,
            run_director=False,
            director_seed=shared,
            run_beat_final=index == len(plan_rows) - 1,
        )
        persisted_id: int | None = None
        persisted_content = ""
        async for event in _consume_pipeline(
            pipeline,
            conversation_id,
            settings,
            current_parent,
            turn_index,
            extra_on_result=_conversation_log_writer(conversation_id, turn_index),
            speaker_member_id=speaker.member_id,
            beat_id=beat_id,
            speaker_name=speaker.name,
            card_id=speaker.card_id,
            emit_done=False,
            world_source_user_msg_id=source_user_message_id,
        ):
            if event["event"] == "speaker_done":
                persisted_id = event["data"].get("message_id")
                persisted_content = event["data"].get("content") or ""
            yield event
        if persisted_id is None:
            break
        grown_history.append(
            {
                "id": persisted_id,
                "role": "assistant",
                "content": persisted_content,
                "turn_index": turn_index,
                "parent_id": current_parent,
                "speaker_member_id": speaker.member_id,
                "beat_id": beat_id,
            }
        )
        current_parent = persisted_id
    yield {"event": "done"}


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_turn(
    conversation_id: str,
    user_message: str,
    skip_user_persist: bool = False,
    attachments: list[dict] | None = None,
    abort_token: AbortToken | None = None,
    speaker_member_id: str | None = None,
) -> AsyncIterator[dict]:
    """Save the user message, run the pipeline, and stream the reply.

    Entry point for ``POST /send`` and ``POST /continue``. For ``/continue``
    (``skip_user_persist=True``) the user row already exists; the pipeline runs
    from there without creating a duplicate.

    Streams: ``user_message_created``, then pipeline events (``director_done``,
    ``token``, ``editor_done``, etc.), and finally ``done``.
    """
    if attachments is None:
        attachments = []

    async def _body(ctx: PipelineContext) -> AsyncIterator[dict]:
        if error := _group_pin_error(ctx, speaker_member_id):
            yield {"event": "error", "data": error}
            return
        # Inline macros ({{roll}}/{{random}}) resolve exactly once, before the
        # row is persisted, so history holds the final text and never re-rolls.
        # For /continue the content came from the DB and is already resolved.
        nonlocal user_message
        user_message = resolve_inline(user_message)

        settings = ctx.settings
        messages = await db.get_messages(conversation_id)
        conv = ctx.conv

        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        # For /continue the user row already exists; use its turn_index.
        user_turn = next_turn

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]
            user_turn = messages[-1]["turn_index"]

        # Read progressive_fields from the grandparent node (branch-aware, unlike conversation_logs).
        ctx.director["progressive_fields"] = progressive.branch_baseline(messages)
        await _load_direction_notes(ctx, conversation_id, messages)

        beat_id: str | None = None
        if not skip_user_persist:
            # Normalize frontend attachment format to DB format before persisting.
            db_attachments = []
            for att in attachments:
                db_attachments.append(
                    {
                        "mime_type": att.get("mime", att.get("mime_type", "image/jpeg")),
                        "data_b64": att.get("b64", att.get("data_b64", "")),
                        "filename": att.get("filename"),
                        "size": att.get("size"),
                    }
                )
            beat_id = str(uuid.uuid4()) if ctx.cast.grouped else None
            user_msg_id, _ = await db.add_message(
                conversation_id,
                "user",
                user_message,
                next_turn,
                parent_id=user_parent_id,
                attachments=db_attachments,
                beat_id=beat_id,
                advance_leaf=ctx.cast.grouped,
            )
            if not ctx.cast.grouped:
                await db.set_active_leaf(conversation_id, user_msg_id)
            # content carries the macro-resolved text so the frontend can sync
            # the optimistic bubble with what was actually persisted.
            yield {"event": "user_message_created", "data": {"id": user_msg_id, "content": user_message}}

        asst_turn = user_turn + 1

        if ctx.cast.grouped:
            if skip_user_persist:
                beat_id = str(messages[-1].get("beat_id") or uuid.uuid4())
            assert beat_id is not None
            async for event in _generate_group_beat(
                ctx,
                conversation_id,
                history=history,
                user_message=user_message,
                attachments=attachments,
                parent_message_id=user_msg_id,
                first_turn_index=asst_turn,
                beat_id=beat_id,
                pinned_speaker_id=speaker_member_id,
                source_user_message_id=user_msg_id,
            ):
                yield event
            return

        # Include the current user message in lorebook scan, not just history.
        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            settings=settings,
            last_user_message=user_message,
            lorebook_messages=history + [{"role": "user", "content": user_message}],
            user_message=user_message,
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=asst_turn,
            log_turn_index=user_turn,
        ):
            yield event

    async for event in _run_turn_handler(conversation_id, abort_token, _body, log_label="Pipeline"):
        yield event


async def handle_speak(
    conversation_id: str,
    speaker_member_id: str,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Generate a pinned group beat without inserting a synthetic user row."""

    async def _body(ctx: PipelineContext) -> AsyncIterator[dict]:
        if not ctx.cast.grouped:
            yield {"event": "error", "data": "Conversation is not a group"}
            return
        messages = await db.get_messages(conversation_id)
        await _load_direction_notes(ctx, conversation_id, messages)
        ctx.director["progressive_fields"] = progressive.branch_baseline(messages)
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0
        async for event in _generate_group_beat(
            ctx,
            conversation_id,
            history=messages,
            user_message="",
            attachments=[],
            parent_message_id=ctx.conv.get("active_leaf_id"),
            first_turn_index=next_turn,
            beat_id=str(uuid.uuid4()),
            pinned_speaker_id=speaker_member_id,
            source_user_message_id=None,
        ):
            yield event

    async for event in _run_turn_handler(conversation_id, abort_token, _body, log_label="Group speak"):
        yield event


async def handle_fork_edit(
    conversation_id: str,
    user_msg_id: int,
    new_content: str,
    abort_token: AbortToken | None = None,
    speaker_member_id: str | None = None,
) -> AsyncIterator[dict]:
    """Fork the conversation at a user message: save the edit and generate a fresh reply.

    Entry point for ``POST /messages/{id}/fork-edit``. Saves the edited text as a
    new sibling of *user_msg_id* (same parent and turn index), resets the director
    to the branch point, then runs the full pipeline. The original message and its
    subtree are left intact; branch navigation shows both.

    Logs at the assistant turn (not the user turn) so this branch's log row is
    distinct from the original turn's log.
    """

    async def _body(ctx: PipelineContext) -> AsyncIterator[dict]:
        if error := _group_pin_error(ctx, speaker_member_id):
            yield {"event": "error", "data": error}
            return
        # Same persist-boundary rule as handle_turn: inline macros in the
        # edited text fire once, before the new sibling row is written.
        nonlocal new_content
        new_content = resolve_inline(new_content)

        settings = ctx.settings
        original = await db.get_message_by_id(user_msg_id)
        if not original or original["conversation_id"] != conversation_id or original["role"] != "user":
            yield {"event": "error", "data": "Invalid target message"}
            return

        parent_id: int | None = original["parent_id"]
        turn_index = original["turn_index"]
        asst_turn = turn_index + 1
        history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []

        # Reset director to branch-point baseline (branch-aware progressive_fields).
        ctx.director["active_moods"] = await db.get_moods_before_turn(conversation_id, turn_index)
        ctx.director["progressive_fields"] = progressive.branch_baseline(history)
        await _load_direction_notes(ctx, conversation_id, history)

        # Carry original attachments onto the new sibling.
        carried_atts = await db.get_user_attachments_for_message(user_msg_id)

        beat_id = str(uuid.uuid4()) if ctx.cast.grouped else None
        new_user_id, _ = await db.add_message(
            conversation_id,
            "user",
            new_content,
            turn_index,
            parent_id=parent_id,
            attachments=carried_atts,
            beat_id=beat_id,
            advance_leaf=ctx.cast.grouped,
        )
        if not ctx.cast.grouped:
            await db.set_active_leaf(conversation_id, new_user_id)
        yield {"event": "user_message_created", "data": {"id": new_user_id, "content": new_content}}

        if ctx.cast.grouped:
            assert beat_id is not None
            async for event in _generate_group_beat(
                ctx,
                conversation_id,
                history=history,
                user_message=new_content,
                attachments=carried_atts,
                parent_message_id=new_user_id,
                first_turn_index=asst_turn,
                beat_id=beat_id,
                pinned_speaker_id=speaker_member_id,
                source_user_message_id=new_user_id,
            ):
                yield event
            return

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            settings=settings,
            last_user_message=new_content,
            lorebook_messages=history + [{"role": "user", "content": new_content}],
            user_message=new_content,
            attachments=carried_atts,
            user_msg_id=new_user_id,
            asst_turn_index=asst_turn,
            log_turn_index=asst_turn,  # log at assistant turn, unlike handle_turn
        ):
            yield event

    async for event in _run_turn_handler(conversation_id, abort_token, _body, log_label="Fork edit"):
        yield event


async def handle_regenerate(
    conversation_id: str,
    assistant_msg_id: int,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Regenerate an assistant message as a new sibling branch.

    Entry point for ``POST /messages/{id}/regenerate``. Resets the director to
    the pre-turn baseline and re-runs the pipeline from the parent user message,
    producing a new reply at the same turn index. The original is kept; branch
    navigation shows both.
    """

    async def _body(ctx: PipelineContext) -> AsyncIterator[dict]:
        settings = ctx.settings
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        user_msg_id = target["parent_id"]
        history, attachments = await _prepare_regen_context(ctx, conversation_id, target, user_msg)

        if ctx.cast.grouped:
            speaker_id = target.get("speaker_member_id")
            if not speaker_id:
                yield {"event": "error", "data": "Target has no group speaker identity"}
                return
            current_message = user_msg["content"] if user_msg.get("role") == "user" else ""
            source_user_id = (
                user_msg_id
                if user_msg.get("role") == "user"
                else next((m.get("id") for m in reversed(history) if m.get("role") == "user"), None)
            )
            async for event in _generate_group_beat(
                ctx,
                conversation_id,
                history=history,
                user_message=current_message,
                attachments=attachments,
                parent_message_id=user_msg_id,
                first_turn_index=target["turn_index"],
                beat_id=str(target.get("beat_id") or uuid.uuid4()),
                pinned_speaker_id=str(speaker_id),
                source_user_message_id=source_user_id,
            ):
                yield event
            return

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            settings=settings,
            last_user_message=user_msg["content"],
            lorebook_messages=[
                *history,
                {"role": "user", "content": user_msg["content"]},
            ],
            user_message=user_msg["content"],
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=target["turn_index"],
            log_turn_index=target["turn_index"],
        ):
            yield event

    async for event in _run_turn_handler(conversation_id, abort_token, _body, log_label="Regenerate"):
        yield event


_SUPER_REGEN_MSG = "[OOC: Your response was kind of meh, rewrite it in a slightly different but still realistic direction.]"


async def _regenerate_with_steering(
    conversation_id: str,
    assistant_msg_id: int,
    steer_msg: str,
    abort_token: AbortToken | None = None,
    *,
    log_label: str = "Steered regenerate",
) -> AsyncIterator[dict]:
    """Regenerate an assistant reply as a new sibling, steered by an OOC message.

    Extends history with the original exchange so the model sees what it wrote,
    then runs the full pipeline with *steer_msg* as the current-turn user
    message: the director reads it when shaping the scene and the writer rewrites
    against it. The original reply is left intact on its own branch.
    """

    async def _body(ctx: PipelineContext) -> AsyncIterator[dict]:
        settings = ctx.settings
        result = await _resolve_target_and_parent(conversation_id, assistant_msg_id)
        if isinstance(result, str):
            yield {"event": "error", "data": result}
            return
        target, user_msg = result

        user_msg_id = target["parent_id"]
        history, attachments = await _prepare_regen_context(ctx, conversation_id, target, user_msg)

        # Include the original parent and target so the model sees what it
        # wrote before being steered. In a group cascade the parent may itself
        # be an assistant and may already be the end of ``history``.
        extended_history = list(history)
        if not extended_history or extended_history[-1].get("id") != user_msg.get("id"):
            extended_history.append(user_msg)
        extended_history.append(target)

        # From history, not extended_history: the reply being replaced is excluded
        # from the audit so the new draft isn't penalised for resembling it.
        editor_audit_msgs = [msg["content"] for msg in reversed(history) if msg.get("role") == "assistant"][
            :AUDIT_BASELINE_WINDOW
        ]

        if ctx.cast.grouped:
            speaker_id = target.get("speaker_member_id")
            if not speaker_id:
                yield {"event": "error", "data": "Target has no group speaker identity"}
                return
            source_user_id = next((m.get("id") for m in reversed(extended_history) if m.get("role") == "user"), None)
            async for event in _generate_group_beat(
                ctx,
                conversation_id,
                history=extended_history,
                user_message=steer_msg,
                attachments=attachments,
                parent_message_id=user_msg_id,
                first_turn_index=target["turn_index"],
                beat_id=str(uuid.uuid4()),
                pinned_speaker_id=str(speaker_id),
                append_user_to_history=False,
                source_user_message_id=source_user_id,
            ):
                yield event
            return

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=extended_history,
            settings=settings,
            last_user_message=user_msg["content"],
            lorebook_messages=extended_history,
            user_message=steer_msg,
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=target["turn_index"],
            log_turn_index=target["turn_index"],
            editor_audit_msgs=editor_audit_msgs,
        ):
            yield event

    async for event in _run_turn_handler(conversation_id, abort_token, _body, log_label=log_label):
        yield event


async def handle_super_regenerate(
    conversation_id: str,
    assistant_msg_id: int,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Regenerate a reply, nudging the model toward a different direction.

    Entry point for ``POST /messages/{id}/super_regenerate``. Sends a canned OOC
    steering message and saves the result as a new sibling branch.
    """
    async for event in _regenerate_with_steering(
        conversation_id, assistant_msg_id, _SUPER_REGEN_MSG, abort_token, log_label="Super-regenerate"
    ):
        yield event


_MAGIC_STEER_PREFIX = "[OOC: Rewrite your previous response above, following this direction: "
_MAGIC_STEER_SUFFIX = ". Keep it consistent with the established scene, characters, and continuity.]"


async def handle_magic_rewrite(
    conversation_id: str,
    assistant_msg_id: int,
    direction: str,
    abort_token: AbortToken | None = None,
) -> AsyncIterator[dict]:
    """Rewrite an assistant reply following a user-supplied direction.

    Entry point for ``POST /messages/{id}/magic_rewrite``. Wraps *direction* in an
    OOC steering message and regenerates as a new sibling branch. The message is
    assembled by concatenation rather than string formatting so braces in
    *direction* are inert (it is macro-resolved downstream).
    """
    steer = _MAGIC_STEER_PREFIX + direction + _MAGIC_STEER_SUFFIX
    async for event in _regenerate_with_steering(
        conversation_id, assistant_msg_id, steer, abort_token, log_label="Magic rewrite"
    ):
        yield event
