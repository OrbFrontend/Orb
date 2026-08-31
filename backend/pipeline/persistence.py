"""Persist pipeline output and turn side effects."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

from .. import database as db
from ..core import resolve_inline
from ..features import lorebook
from ..workflows.attachment_cache import project_rejected_attachment
from .predicates import agent_enabled
from .state import TurnState

logger = logging.getLogger(__name__)


def _conversation_log_writer(conversation_id: str, log_turn_index: int):
    """Return an async callback that writes the ``conversation_logs`` row for this turn.

    The callback runs right after the assistant message is saved. Normal turns
    log at the user turn index; branch-creating paths (fork-edit, regenerate)
    log at the assistant turn index so their log rows stay distinguishable.
    """

    async def _on_result(res: TurnState, asst_id):
        await db.add_conversation_log(
            conversation_id,
            log_turn_index,
            res.calls,
            res.active_moods,
            res.inj_block,
            res.latency,
            message_id=asst_id,
            reasoning_director=res.reasoning_director,
            reasoning_writer=res.reasoning_writer,
            reasoning_editor=res.reasoning_editor,
            feedback=res.feedback_values,
        )

    return _on_result


async def _stage_world_proposals(res: TurnState, user_msg_id: int | None, asst_id: int) -> list[dict]:
    """Persist the turn's validated proposals as pending changesets, one per World.

    Runs at the same boundary as the assistant message and immediately after it,
    because a changeset names its source messages and only now is the assistant
    row's id known. Returns one compact payload per staged changeset for the
    ``world_change_proposed`` event, in World order.

    Failures are swallowed *per World*: the reply is already committed, a
    bookkeeping write must not turn a finished turn into a failed one, and one
    World failing is no reason to drop another's proposal.
    """
    payloads: list[dict] = []
    for proposal in res.world_proposals:
        try:
            changeset = await lorebook.stage_proposal(
                proposal,
                source_user_message_id=user_msg_id,
                source_assistant_message_id=asst_id,
            )
        except Exception:
            logger.exception(
                "Failed to stage world change proposal for world %s on assistant message %s",
                proposal.get("world_id"),
                asst_id,
            )
            continue
        payloads.append({"message_id": asst_id, "changeset": changeset})
    return payloads


async def _persist_result(
    conversation_id: str,
    res: TurnState,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    speaker_member_id: str | None = None,
    exchange_id: str | None = None,
    world_source_user_msg_id: int | None = None,
) -> tuple[int | None, list[dict], list[dict]]:
    """Persist the assistant message and turn side effects."""
    if agent_enabled(settings):
        await db.update_director_state(
            conversation_id,
            res.active_moods,
            progressive_fields=res.progressive_fields,
            macro_choices=res.macro_choices,
        )

    # Skip persistence if the LLM produced no content tokens (e.g. reasoning-only).
    # Inline macros the model emitted (copied from context) are already frozen by
    # the time they get here: the writer stage resolves them the moment streaming
    # ends, so the retained ``writer_draft`` and every post-writer pass read the
    # same settled text. This call is the backstop for the paths that do not run
    # the writer stage, and a no-op for the ones that do — a resolved string has
    # no macros left to roll.
    #
    # Written back onto *res*, not kept local: a group exchange replays this reply
    # to the next speaker straight off the ``speaker_done`` event, so that event has
    # to carry the same text the row holds. Resolving a second time downstream would
    # roll the dice again and hand the later speakers a number the DB never had —
    # the next request would then read a different byte at that history position and
    # re-prefill everything after it.
    res.resp_text = resolve_inline(res.resp_text)
    resp_text = res.resp_text
    if resp_text.strip():
        # Attachments ride the same INSERT transaction; aborted turns leave no orphans.
        staged = res.staged_attachments or None
        asst_id, rejected = await db.add_message(
            conversation_id,
            "assistant",
            resp_text,
            turn_index,
            parent_id=user_msg_id,
            attachments=staged,
            progressive_fields=res.progressive_fields,
            speaker_member_id=speaker_member_id,
            exchange_id=exchange_id,
            # Writer stage freezes inline macros before it captures this, so it
            # is the same stable, human-readable source the in-turn local
            # rewriter received.
            writer_draft=res.writer_draft or resp_text,
            advance_leaf=True,
        )
        # Row id only known here; no other caller can name it yet, so no lock needed.
        for wid, payload in res.staged_message_state.items():
            try:
                await db.set_workflow_message_state(asst_id, wid, payload)
            except Exception:
                logger.exception(
                    "Failed to persist workflow message state (wid=%r) for assistant message %s; "
                    "row already committed, continuing",
                    wid,
                    asst_id,
                )
        # Counter seed scans existing rows, so this must run after add_message.
        try:
            await db.add_generated_chars(len(resp_text))
        except Exception:
            logger.exception("Failed to update generated-chars counter; row already committed")
        if res.direction_notes:
            try:
                await db.create_direction_notes(conversation_id, asst_id, res.direction_notes)
            except Exception:
                logger.exception("Failed to persist direction notes for assistant message %s; row already committed", asst_id)
        proposals = await _stage_world_proposals(res, world_source_user_msg_id, asst_id)
        return asst_id, rejected, proposals
    else:
        logger.info("Skipping assistant message persistence: resp_text is empty (reasoning‑only output)")
        if res.direction_notes:
            logger.info("Dropping %d direction note(s): turn produced no assistant message", len(res.direction_notes))
        if res.world_proposals:
            logger.info(
                "Dropping %d world change proposal(s): turn produced no assistant message to anchor them to",
                len(res.world_proposals),
            )
        return None, [], []


async def _fallback_persist(
    conversation_id: str,
    res: TurnState,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
    speaker_member_id: str | None = None,
    exchange_id: str | None = None,
):
    """Best-effort save for a turn aborted before ``_result`` fired.

    Saves whatever the writer streamed (``accumulated_text``) if non-empty.
    Reasoning-only output does not create a message node. Errors are swallowed
    so a save failure never propagates to the caller.
    """
    try:
        if res.active_moods and agent_enabled(settings):
            await db.update_director_state(
                conversation_id,
                res.active_moods,
                progressive_fields=res.progressive_fields,
                macro_choices=res.macro_choices,
            )

        # accumulated_text holds only writer tokens (not reasoning deltas).
        # Same persist-boundary macro resolution as _persist_result.
        accumulated_text = resolve_inline(accumulated_text)
        if accumulated_text.strip():
            asst_id, _ = await db.add_message(
                conversation_id,
                "assistant",
                accumulated_text,
                turn_index,
                parent_id=user_msg_id,
                speaker_member_id=speaker_member_id,
                exchange_id=exchange_id,
                # The writer stage did not finish on this abort path, so its
                # macro-frozen partial text is the closest retained draft.
                writer_draft=accumulated_text,
                advance_leaf=True,
            )
            logger.info(
                "Fallback persistence saved incomplete assistant message (%d chars)",
                len(accumulated_text),
            )
    except Exception:
        logger.exception("Fallback persistence failed")


async def _shielded_fallback(
    conversation_id: str,
    res: TurnState,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
    speaker_member_id: str | None = None,
    exchange_id: str | None = None,
):
    """Run :func:`_fallback_persist` under ``asyncio.shield``, retrying once on cancellation.

    Ensures partial output is saved even when the request task is cancelled mid-write.
    """
    try:
        await asyncio.shield(
            _fallback_persist(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
                speaker_member_id,
                exchange_id,
            )
        )
    except asyncio.CancelledError:
        try:
            await _fallback_persist(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
                speaker_member_id,
                exchange_id,
            )
        except Exception:
            logger.exception("Fallback persistence retry failed")


async def _shielded_log_save(extra_on_result, res: TurnState, asst_id: int | None):
    """Run the ``extra_on_result`` callback exactly once under ``asyncio.shield``.

    The callback writes a ``conversation_logs`` row (a bare INSERT with no dedup
    guard). Cancellation is not retried — a partial write already committed the
    row, and re-running would create a duplicate. Non-cancel errors are swallowed
    so a log failure never crashes the turn.
    """

    async def _run():
        try:
            await extra_on_result(res, asst_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to save conversation log")

    await asyncio.shield(_run())


async def _consume_pipeline(
    pipeline: AsyncIterator[dict],
    conversation_id: str,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    *,
    extra_on_result=None,
    speaker_member_id: str | None = None,
    exchange_id: str | None = None,
    speaker_name: str = "",
    card_id: str | None = None,
    emit_done: bool = True,
    world_source_user_msg_id: int | None = None,
) -> AsyncIterator[dict]:
    """Drain the pipeline's SSE events, save results, and emit ``done``.

    Passes ``token`` and all other public events straight to the caller. When
    the ``_result`` event arrives, saves the assistant message and calls the
    optional *extra_on_result* callback ``(res, asst_id) -> None`` (used to
    write the conversation log).

    Falls back to partial persistence in the ``finally`` block if the pipeline
    exits before ``_result`` fires (abort or error).
    """
    res = TurnState()
    asst_id = None
    persisted = False
    accumulated_text = ""

    try:
        async for event in pipeline:
            etype = event["event"]
            if etype == "token":
                accumulated_text += event["data"]
                yield event
            elif etype == "_result":
                res = TurnState(**event["data"])
                asst_id, rejected, proposals = await _persist_result(
                    conversation_id,
                    res,
                    settings,
                    user_msg_id,
                    turn_index,
                    speaker_member_id=speaker_member_id,
                    exchange_id=exchange_id,
                    world_source_user_msg_id=world_source_user_msg_id,
                )
                persisted = True
                for proposal in proposals:
                    # Ordered before `done` on purpose: the frontend paints the
                    # proposal cards from the same repaint that finalises the reply.
                    # One event per World -- the payload names a single changeset.
                    yield {"event": "world_change_proposed", "data": proposal}
                if rejected and asst_id is not None:
                    # originating_attachment_id is None (first-write rejection, no DB row).
                    yield {
                        "event": "workflow_attachments_rejected",
                        "data": {
                            "message_id": asst_id,
                            "rejected": [project_rejected_attachment(a, None) for a in rejected],
                        },
                    }
            else:
                yield event
    finally:
        # Runs on every exit path (normal, exception, cancellation) exactly once.
        if not persisted:
            await _shielded_fallback(
                conversation_id,
                res,
                settings,
                user_msg_id,
                turn_index,
                accumulated_text,
                speaker_member_id,
                exchange_id,
            )
        elif extra_on_result:
            await _shielded_log_save(extra_on_result, res, asst_id)

    if exchange_id is not None and speaker_member_id is not None:
        yield {
            "event": "speaker_done",
            "data": {
                "exchange_id": exchange_id,
                "message_id": asst_id,
                "parent_id": user_msg_id,
                "turn_index": turn_index,
                "member_id": speaker_member_id,
                "card_id": card_id,
                "name": speaker_name,
                # Post-persist ``res.resp_text``: ``_persist_result`` resolved the
                # inline macros in place, so this is the text the row holds and the
                # text the exchange driver replays to the next speaker. The fallback
                # branch persisted nothing (``message_id`` is None), which ends the
                # exchange, so its text never reaches another speaker's history.
                "content": res.resp_text if persisted else accumulated_text,
            },
        }
    if emit_done:
        yield {"event": "done"}
