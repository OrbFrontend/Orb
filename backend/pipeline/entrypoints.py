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
from typing import Any, AsyncIterator, Callable, List, Mapping, Optional, Sequence

from .. import database as db
from ..inference import AbortToken
from .context import (
    PipelineContext,
    _load_pipeline_context,
    _prepare_turn,
    _TurnSetup,
)
from .orchestrator import _run_pipeline
from .passes.director import progressive
from .passes.director.prompt_rewrite import disable_rewrite
from .passes.editor.editor import AUDIT_BASELINE_WINDOW
from .persistence import _consume_pipeline, _conversation_log_writer

logger = logging.getLogger(__name__)


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
    place defines the error wire contract for every handler.
    """
    try:
        ctx = await _load_pipeline_context(conversation_id, abort_token=abort_token)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}
            return
        async for event in body(ctx):
            yield event
    except Exception:
        logger.exception("%s error", log_label)
        yield {"event": "error", "data": "Generation failed; see server logs"}


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
    """Load an assistant message and its parent user message.

    Returns ``(target, user_msg)`` on success, or an error string if the
    message is missing, belongs to a different conversation, or is not an
    assistant message.
    """
    target = await db.get_message_by_id(assistant_msg_id)
    if not target or target["conversation_id"] != conversation_id or target["role"] != "assistant":
        return "Invalid target message"
    user_msg_id = target["parent_id"]
    user_msg = await db.get_message_by_id(user_msg_id) if user_msg_id else None
    if not user_msg:
        return "Parent user message not found"
    return target, user_msg


async def _prepare_regen_context(
    ctx: PipelineContext,
    conversation_id: str,
    target: Mapping[str, Any],
    user_msg: Mapping[str, Any],
) -> tuple[Sequence[Mapping[str, Any]], Sequence[Mapping[str, Any]]]:
    """Load history and attachments for a regeneration, and reset the director.

    Resets the director's active moods and progressive fields to the pre-turn
    baseline so the regenerated reply starts from the same state as the original.
    Returns ``(history, attachments)``.
    """
    parent_id: int | None = user_msg.get("parent_id")
    history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []
    moods_before = await db.get_moods_before_turn(conversation_id, target["turn_index"] - 1)
    ctx.director["active_moods"] = moods_before
    ctx.director["progressive_fields"] = progressive.branch_baseline(history)
    await _load_direction_notes(ctx, conversation_id, history)
    user_msg_id = target["parent_id"]
    attachments = await db.get_user_attachments_for_message(user_msg_id) if user_msg_id else []
    return history, attachments


async def _generate_reply(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    pipeline_settings: Mapping[str, Any],
    last_user_message: str,
    lorebook_messages: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Sequence[Mapping[str, Any]],
    user_msg_id: int | None,
    asst_turn_index: int,
    log_turn_index: int,
    editor_audit_msgs: list[str] | None = None,
    consume_settings: Mapping[str, Any] | None = None,
) -> AsyncIterator[dict]:
    """Run setup → pipeline → persist and stream all SSE events.

    The user message row must already be persisted before this is called.

    *pipeline_settings* drives the passes; *consume_settings* (defaults to the
    same) is used during persistence. They differ for the steered-regenerate
    paths (super-regenerate and magic-rewrite), which pass a rewrite-disabled
    copy to the pipeline but persist under the original settings. *user_message*
    is what the writer actually receives; it may differ from *last_user_message*
    (the steered paths send an OOC message as the writer input while
    *last_user_message* carries the original).
    """
    setup: _TurnSetup | None = None
    async for ev in _prepare_turn(
        ctx,
        conversation_id,
        history=history,
        settings=pipeline_settings,
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
        pipeline_settings,
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
    )
    async for event in _consume_pipeline(
        pipeline,
        conversation_id,
        consume_settings if consume_settings is not None else pipeline_settings,
        user_msg_id,
        asst_turn_index,
        extra_on_result=_conversation_log_writer(conversation_id, log_turn_index),
    ):
        yield event


# ═══════════════════════════════════════════════════════════════════════════════
# Public entry points
# ═══════════════════════════════════════════════════════════════════════════════


async def handle_turn(
    conversation_id: str,
    user_message: str,
    skip_user_persist: bool = False,
    attachments: Optional[List[dict]] = None,
    abort_token: AbortToken | None = None,
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
            user_msg_id, _ = await db.add_message(
                conversation_id,
                "user",
                user_message,
                next_turn,
                parent_id=user_parent_id,
                attachments=db_attachments,
            )
            await db.set_active_leaf(conversation_id, user_msg_id)
            yield {"event": "user_message_created", "data": {"id": user_msg_id}}

        asst_turn = user_turn + 1

        # Include the current user message in lorebook scan, not just history.
        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            pipeline_settings=settings,
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


async def handle_fork_edit(
    conversation_id: str,
    user_msg_id: int,
    new_content: str,
    abort_token: AbortToken | None = None,
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

        new_user_id, _ = await db.add_message(
            conversation_id,
            "user",
            new_content,
            turn_index,
            parent_id=parent_id,
            attachments=carried_atts,
        )
        await db.set_active_leaf(conversation_id, new_user_id)
        yield {"event": "user_message_created", "data": {"id": new_user_id}}

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            pipeline_settings=settings,
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

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=history,
            pipeline_settings=settings,
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
    against it. The prompt-rewrite tool is disabled so the director cannot alter
    the steering message. The original reply is left intact on its own branch.
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

        # Include the original exchange so the model sees what it wrote before being steered.
        extended_history = [
            *history,
            {"role": "user", "content": user_msg["content"]},
            {"role": "assistant", "content": target["content"]},
        ]
        steer_settings = {
            **settings,
            "enabled_tools": disable_rewrite(settings.get("enabled_tools") or {}),
        }

        # From history, not extended_history: the reply being replaced is excluded
        # from the audit so the new draft isn't penalised for resembling it.
        editor_audit_msgs = [msg["content"] for msg in reversed(history) if msg.get("role") == "assistant"][
            :AUDIT_BASELINE_WINDOW
        ]

        async for event in _generate_reply(
            ctx,
            conversation_id,
            history=extended_history,
            pipeline_settings=steer_settings,
            last_user_message=user_msg["content"],
            lorebook_messages=extended_history,
            user_message=steer_msg,
            attachments=attachments,
            user_msg_id=user_msg_id,
            asst_turn_index=target["turn_index"],
            log_turn_index=target["turn_index"],
            editor_audit_msgs=editor_audit_msgs,
            consume_settings=settings,
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
