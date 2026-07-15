"""
persistence.py — Saves the turn output after the pipeline finishes.

:func:`_consume_pipeline` drains the pipeline's SSE events, passes public ones
through to the caller, and on the terminal ``_result`` event writes the assistant
message and all turn side-effects (director state, workflow attachments,
per-message state, active-leaf advance, lifetime char counter, conversation log).

The ``_persist_*`` / ``_fallback_*`` / ``_shielded_*`` helpers separate the happy
path from the best-effort save triggered when a turn is aborted before
``_result`` fires, with ``asyncio.shield`` protecting the finally-block writes
from request-task cancellation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Mapping

from .. import database as db
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
            res.agent_raw,
            res.calls,
            res.active_moods,
            res.inj_block,
            res.latency,
            res.progressive_fields,
            message_id=asst_id,
            reasoning_director=res.reasoning_director,
            reasoning_writer=res.reasoning_writer,
            reasoning_editor=res.reasoning_editor,
            feedback=res.feedback_values,
        )

    return _on_result


async def _persist_result(
    conversation_id: str,
    res: TurnState,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
) -> tuple[int | None, list[dict]]:
    """Save the assistant message and all turn side-effects after ``_result`` fires.

    Updates director state, saves the assistant message with any workflow
    attachments, writes per-message workflow state, advances the active leaf,
    and increments the lifetime character counter.

    Returns ``(asst_id, rejected_workflow_atts)``. ``rejected_workflow_atts``
    is non-empty when the attachment cache dropped entries that lacked the
    metadata needed for re-synthesis.
    """
    if agent_enabled(settings):
        await db.update_director_state(
            conversation_id,
            res.active_moods,
            progressive_fields=res.progressive_fields,
        )

    # Skip persistence if the LLM produced no content tokens (e.g. reasoning-only).
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
        try:
            await db.set_active_leaf(conversation_id, asst_id)
        except Exception:
            logger.exception(
                "Failed to set active leaf to assistant message %s; row already committed",
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
        return asst_id, rejected
    else:
        logger.info("Skipping assistant message persistence: resp_text is empty (reasoning‑only output)")
        if res.direction_notes:
            logger.info("Dropping %d direction note(s): turn produced no assistant message", len(res.direction_notes))
        return None, []


async def _fallback_persist(
    conversation_id: str,
    res: TurnState,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    accumulated_text: str,
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
            )

        # accumulated_text holds only writer tokens (not reasoning deltas).
        if accumulated_text.strip():
            asst_id, _ = await db.add_message(
                conversation_id,
                "assistant",
                accumulated_text,
                turn_index,
                parent_id=user_msg_id,
            )
            await db.set_active_leaf(conversation_id, asst_id)
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
                asst_id, rejected = await _persist_result(conversation_id, res, settings, user_msg_id, turn_index)
                persisted = True
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
            )
        elif extra_on_result:
            await _shielded_log_save(extra_on_result, res, asst_id)

    yield {"event": "done"}
