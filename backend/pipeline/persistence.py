"""Persist pipeline output and turn side effects."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from .. import database as db
from ..core import resolve_inline
from ..features import lorebook
from ..workflows.attachment_cache import project_rejected_attachment
from .events import HookEvent, PipelineEvent, PublicTurnEvent, WorldChangeData
from .failures import STAGE_SAVE, mark_stage
from .predicates import agent_enabled
from .state import TurnState

logger = logging.getLogger(__name__)

ResultCallback = Callable[[TurnState, int | None], Awaitable[None]]

# What ``_persist_result`` returns: the assistant row id, rejected attachments, and the staged World proposals.
_Saved = tuple[int | None, list[dict], list[WorldChangeData]]


def conversation_log_writer(conversation_id: str, log_turn_index: int) -> ResultCallback:
    """Return an async callback that writes the ``conversation_logs`` row for this turn.

    The callback runs right after the assistant message is saved. Normal turns log at the user turn index; branch-creating paths
    (fork-edit, regenerate) log at the assistant turn index so their log rows stay distinguishable.
    """

    async def _on_result(res: TurnState, asst_id: int | None) -> None:
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
            state_report=res.state_report if any(res.state_report.values()) else None,
        )

    return _on_result


async def _stage_world_proposals(res: TurnState, user_msg_id: int | None, asst_id: int) -> list[WorldChangeData]:
    """Stage validated proposals after assistant persistence, returning event payloads in World order.

    Log failures per World without failing the already committed reply or dropping other proposals.
    """
    payloads: list[WorldChangeData] = []
    for proposal in res.world_proposals:
        try:
            changeset = await lorebook.stage_proposal(
                proposal, source_user_message_id=user_msg_id, source_assistant_message_id=asst_id
            )
        except Exception:
            logger.exception(
                "Failed to stage world change proposal for world %s on assistant message %s", proposal.get("world_id"), asst_id
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
) -> _Saved:
    """Persist the assistant message and turn side effects.

    A turn with no reply text commits nothing, the Director's moods included: a regeneration starts from the branch's earlier
    moods, and an empty attempt (stopped before any prose) must not write those over the committed ones.
    """
    # Skip empty drafts. Inline macros normally freeze after Writer streaming; this is a backstop for other paths. Update res so
    # speaker_done and the DB share settled text, avoiding rerolls and cross-turn prefix drift.
    res.resp_text = resolve_inline(res.resp_text)
    resp_text = res.resp_text
    if resp_text.strip():
        if agent_enabled(settings):
            await db.update_director_state(conversation_id, res.active_moods, macro_choices=res.macro_choices)
        # Attachments ride the same INSERT transaction; aborted turns leave no orphans.
        staged = res.staged_attachments or None
        asst_id, rejected = await db.add_message(
            conversation_id,
            "assistant",
            resp_text,
            turn_index,
            parent_id=user_msg_id,
            attachments=staged,
            fragment_cooldowns=res.fragment_cooldowns,
            # Commit decisions and cooldowns with the reply so partial output remains replayable.
            decision_evaluations=res.decision_evaluations,
            decision_cooldowns=res.decision_cooldowns,
            # The state the reply produced commits in the same transaction, so a saved reply and its state cannot diverge.
            state_events=res.state_events,
            speaker_member_id=speaker_member_id,
            exchange_id=exchange_id,
            # Captured after Editor and before Prose Rewriter, so an on-demand
            # rewrite starts from the same edited source as the in-turn pass.
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
        proposals = await _stage_world_proposals(res, world_source_user_msg_id, asst_id)
        return asst_id, rejected, proposals
    else:
        logger.info("Skipping assistant message persistence: resp_text is empty (reasoning‑only output)")
        if res.state_events:
            logger.info("Dropping %d state change(s): turn produced no assistant message", len(res.state_events))
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
    *,
    speaker_member_id: str | None,
    exchange_id: str | None,
    world_source_user_msg_id: int | None,
    extra_on_result: ResultCallback | None,
) -> Exception | None:
    """Best-effort persist of live state when the pipeline exits before _result.

    Save the authoritative draft and accepted turn state/logs; no draft means
    no message. Return logged errors so the caller chooses which failure to report.
    """
    try:
        if not res.resp_text.strip():
            logger.info("Fallback persistence: the turn produced no reply text; nothing saved")
            return None
        asst_id, _, _ = await _persist_result(
            conversation_id,
            res,
            settings,
            user_msg_id,
            turn_index,
            speaker_member_id=speaker_member_id,
            exchange_id=exchange_id,
            world_source_user_msg_id=world_source_user_msg_id,
        )
        logger.info("Fallback persistence saved incomplete assistant message (%d chars)", len(res.resp_text))
    except Exception as exc:
        logger.exception("Fallback persistence failed")
        return exc
    if extra_on_result:
        try:
            await extra_on_result(res, asst_id)
        except Exception:
            logger.exception("Failed to save conversation log")
    return None


async def _shielded_fallback(*args: Any, **kwargs: Any) -> Exception | None:
    """Run :func:`_fallback_persist` to completion even if the request is cancelled.

    ``asyncio.shield`` keeps the save running when the awaiting task is cancelled, so a cancellation waits on that same task
    once more instead of starting a second save -- a second run would insert the reply twice.
    """
    task = asyncio.ensure_future(_fallback_persist(*args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        return await task


async def _finish_save(task: asyncio.Future[_Saved]) -> _Saved:
    """Await the reply's one save through a cancellation of the caller.

    The save runs past the INSERT's commit (message state, counters, World proposals), so a cancellation there must not abandon
    it half-way or let a fallback insert the reply again. The caller waits for the same task, then its cancellation continues.
    """
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if not task.done():
            with contextlib.suppress(Exception):
                await task
        raise


async def _shielded_log_save(extra_on_result: ResultCallback, res: TurnState, asst_id: int | None) -> None:
    """Run the ``extra_on_result`` callback exactly once under ``asyncio.shield``.

    The callback writes a ``conversation_logs`` row (a bare INSERT with no dedup guard). Cancellation is not retried -- a partial
    write already committed the row, and re-running would create a duplicate. Non-cancel errors are swallowed so a log failure
    never crashes the turn.
    """

    async def _run():
        try:
            await extra_on_result(res, asst_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to save conversation log")

    await asyncio.shield(_run())


async def consume_pipeline(
    pipeline: AsyncIterator[PipelineEvent],
    conversation_id: str,
    settings: Mapping[str, Any],
    user_msg_id: int | None,
    turn_index: int,
    *,
    extra_on_result: ResultCallback | None = None,
    speaker_member_id: str | None = None,
    exchange_id: str | None = None,
    speaker_name: str = "",
    card_id: str | None = None,
    emit_done: bool = True,
    world_source_user_msg_id: int | None = None,
) -> AsyncIterator[PublicTurnEvent]:
    """Forward pipeline events, persist _result, then emit done.

    After saving, call extra_on_result(res, asst_id) if supplied. Finally saves live state on abort/error before _result. Save
    at most once; mark and raise save failures as STAGE_SAVE even on stopped turns.
    """
    res = TurnState()
    asst_id = None
    persisted = False
    accumulated_text = ""
    # The pipeline's live working state, announced ahead of the Writer, for the
    # fallback save of a turn that ends before ``_result``.
    live: TurnState | None = None
    # The ``_result`` save, once started. Its INSERT may commit before a
    # cancellation lands, so from then on the fallback never runs.
    saving: asyncio.Future[_Saved] | None = None
    exiting: BaseException | None = None

    try:
        async for event in pipeline:
            if isinstance(event, HookEvent):
                yield event
                continue
            if event["event"] == "token":
                accumulated_text += event["data"]
                yield event
            elif event["event"] == "_turn_state":
                live = event["data"]
            elif event["event"] == "_result":
                res = TurnState(**event["data"])
                saving = asyncio.ensure_future(
                    _persist_result(
                        conversation_id,
                        res,
                        settings,
                        user_msg_id,
                        turn_index,
                        speaker_member_id=speaker_member_id,
                        exchange_id=exchange_id,
                        world_source_user_msg_id=world_source_user_msg_id,
                    )
                )
                try:
                    asst_id, rejected, proposals = await _finish_save(saving)
                except Exception as exc:
                    mark_stage(exc, STAGE_SAVE)
                    raise
                persisted = True
                for proposal in proposals:
                    # Ordered before `done` on purpose: the frontend paints the proposal cards from the same repaint that
                    # finalises the reply. One event per World -- the payload names a single changeset.
                    yield {"event": "world_change_proposed", "data": proposal}
                if rejected and asst_id is not None:
                    # originating_attachment_id is None (first-write rejection, no DB row).
                    yield {
                        "event": "workflow_attachments_rejected",
                        "data": {"message_id": asst_id, "rejected": [project_rejected_attachment(a, None) for a in rejected]},
                    }
            else:
                yield event
    except BaseException as exc:
        exiting = exc
        raise
    finally:
        # Runs on every exit path (normal, exception, cancellation) exactly once.
        # Cancelled while the save finished: keep its result for the log row.
        if saving is not None and not persisted and saving.done() and not saving.cancelled() and saving.exception() is None:
            asst_id, _, _ = saving.result()
            persisted = True
        if saving is None:
            save_error = await _shielded_fallback(
                conversation_id,
                live if live is not None else TurnState(resp_text=accumulated_text),
                settings,
                user_msg_id,
                turn_index,
                speaker_member_id=speaker_member_id,
                exchange_id=exchange_id,
                world_source_user_msg_id=world_source_user_msg_id,
                extra_on_result=extra_on_result,
            )
            # The reply is lost, which outweighs whatever ended the turn (that
            # stays the cause). A closed or cancelled stream has nowhere to say it.
            if save_error is not None and not isinstance(exiting, (GeneratorExit, asyncio.CancelledError)):
                mark_stage(save_error, STAGE_SAVE)
                raise save_error from exiting
        elif persisted and extra_on_result:
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
                # Post-persist ``res.resp_text``: ``_persist_result`` resolved the inline macros in place, so this is the text
                # the row holds and the text the exchange driver replays to the next speaker. The fallback branch persisted
                # nothing (``message_id`` is None), which ends the exchange, so its text never reaches another speaker's
                # history.
                "content": res.resp_text if persisted else accumulated_text,
            },
        }
    if emit_done:
        yield {"event": "done"}
