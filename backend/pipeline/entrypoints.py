"""Public turn handlers and the shared generation driver."""

from __future__ import annotations

import logging
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Any

from .. import database as db
from ..core import card_description, resolve_inline
from ..inference import AbortToken, DecisionCancelled
from ..prompting import prefix_is_speaker_scoped, tail_carries_identity
from .cast import parse_speaking_plan, plan_cue, round_robin_member
from .config import _resolve_pipeline_config
from .context import (
    PipelineContext,
    _build_prefixes,
    _load_pipeline_context,
    _prepare_turn,
    _TurnSetup,
)
from .failures import STAGE_JUDGE, describe_failure, staged
from .orchestrator import _run_pipeline, open_turn_state, run_director_stage
from .passes.director import cooldown
from .passes.editor.editor import AUDIT_BASELINE_WINDOW
from .passes.judge import (
    JudgeResult,
    JudgeTurn,
    build_snapshot,
    card_snapshots,
    decision_cooldown_baseline,
    judge_pass,
    stored_evaluations,
)
from .persistence import _consume_pipeline, _conversation_log_writer
from .state import SheetUpdateTurn, empty_state_report

logger = logging.getLogger(__name__)


def _history_attachments(attachments: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Re-key uploads into the shape history rows carry.

    The wire format (``mime``/``b64``) reaches a turn straight from the browser,
    while ``format_message_with_attachments`` reads history rows through the DB
    names. A later speaker in an exchange sees the user's image only as part of the
    replayed history — not as its own trailing attachment — so the two spellings
    have to meet here or the picture silently stops at the first speaker.
    """
    out: list[dict] = []
    for att in attachments:
        b64 = att.get("data_b64") or att.get("b64") or ""
        if not b64:
            continue
        out.append({"mime_type": att.get("mime_type") or att.get("mime") or "image/jpeg", "data_b64": b64})
    return out


# The largest round any strategy can legitimately schedule (``group_max_speakers``
# is capped at 8). A chip-click scene can chain requests without ever inserting a
# user row, so the lookback needs a ceiling that is not "the whole conversation".
_ROUND_MAX_REPLIES = 8


def _round_prefix(
    history: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
) -> tuple[str, list[tuple[str, str]]]:
    """The round already on the branch: the user's last message and every reply since.

    An exchange is *request*-scoped. Under `manual` — and for any cast-chip click on a
    resting scene — one round is several requests, so this request's own replies
    are not the round. The sheet pass would otherwise be asked "did this exchange
    durably change Kael?" with the line that changed him in a different request,
    and on `handle_speak` with no user message at all. This is the same round
    ``workflows/image_gen/subjects.py`` reads, for the same reason, and it is
    consulted only when the request did not bring a user message of its own.
    """
    lines: list[tuple[str, str]] = []
    user_message = ""
    for row in reversed(history):
        if row.get("role") == "user":
            user_message = str(row.get("content") or "")
            break
        if row.get("role") != "assistant":
            continue
        content = str(row.get("content") or "")
        if content.strip():
            lines.append((names.get(str(row.get("speaker_member_id") or "")) or "Speaker", content))
        if len(lines) >= _ROUND_MAX_REPLIES:
            break
    lines.reverse()
    return user_message, lines


def _group_pin_error(ctx: PipelineContext, pinned_speaker_id: str | None) -> str | None:
    """Return an error when a group pin names no member."""
    if not ctx.cast.grouped:
        return None
    eligible = [member for member in ctx.group_members if not member.get("muted")]
    if pinned_speaker_id and not any(member["id"] == pinned_speaker_id for member in eligible):
        return "Pinned speaker is not active and unmuted"
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


async def _run_judge(
    ctx: PipelineContext,
    *,
    history: Sequence[Mapping[str, Any]],
    current_request: str,
    macros: Any,
    anchor_message_id: int | None,
) -> AsyncIterator[dict | JudgeResult]:
    prior = ctx.director.get("decision_cooldowns") or {}
    snapshot = build_snapshot(
        history=history,
        current_request=current_request,
        macros=macros,
        scope="group" if ctx.cast.grouped else "solo",
        speaker_names=ctx.speaker_names,
        scripts=ctx.card_scripts,
        speaker_scripts=ctx.speaker_scripts,
        description=None if ctx.cast.grouped else card_description(ctx.card),
        anchor_message_id=anchor_message_id,
    )
    turn = JudgeTurn(
        snapshot=snapshot,
        card_snapshots=card_snapshots(snapshot, ctx.cast.members) if ctx.cast.grouped else {},
        candidates=ctx.decision_candidates,
        config=ctx.judge_config,
        prior_cooldowns=prior,
        replay_records=tuple(ctx.director.get("decision_replay") or ()),
        invalid=ctx.invalid_decisions,
    )
    has_work = bool(turn.candidates or turn.invalid)
    if has_work:
        yield {"event": "step_start", "data": {"step": "judge"}}
    try:
        result = await judge_pass(turn, abort=ctx.client.abort_token)
    except DecisionCancelled:
        logger.info("Judge pass cancelled by stop")
        yield JudgeResult(cooldowns=dict(prior))
        return
    if has_work:
        yield {"event": "decisions", "data": result.as_event_data()}
    yield result


def _exchange_decision_input(
    history: Sequence[Mapping[str, Any]],
    user_message: str,
    parent_message_id: int | None,
    *,
    exchange_id: str | None,
    steering: str = "",
) -> tuple[Sequence[Mapping[str, Any]], str, int | None]:
    """Return the exchange's history, request, and anchor for decision replay.

    Regeneration rewinds to the exchange start. A turn without its own request
    reuses the pending user message, and steering is appended to the request.
    """
    before, request, anchor = history, user_message, parent_message_id
    if exchange_id is not None:
        first = next((index for index, row in enumerate(history) if str(row.get("exchange_id") or "") == exchange_id), None)
        if first is None:
            return history, user_message, parent_message_id
        row = history[first]
        before = history[:first]
        request = str(row.get("content") or "") if row.get("role") == "user" else ""
        anchor = row.get("id") if row.get("role") == "user" else (before[-1]["id"] if before else None)
    if not request and before and before[-1].get("role") == "user":
        pending = before[-1]
        before, request, anchor = before[:-1], str(pending.get("content") or ""), pending.get("id")
    return before, "\n\n".join(part for part in (request, steering) if part), anchor


def _committed_exchange_decisions(
    history: Sequence[Mapping[str, Any]], parent_message_id: int | None, exchange_id: str | None
) -> JudgeResult | None:
    """Return the committed result when the parent belongs to this exchange."""
    if exchange_id is None or parent_message_id is None:
        return None
    parent = next((row for row in reversed(history) if row.get("id") == parent_message_id), None)
    if parent is None or parent.get("role") != "assistant" or str(parent.get("exchange_id") or "") != exchange_id:
        return None
    return JudgeResult.committed(db.decision_evaluations_of(parent), parent.get("decision_cooldowns") or {})


async def _load_fragment_state(
    ctx: PipelineContext,
    conversation_id: str,
    path: Sequence[Mapping[str, Any]],
    *,
    replacing: Mapping[str, Any] | None = None,
) -> None:
    """Seed ``ctx.director`` with the branch's state-fragment state.

    ``fragment_state`` folds every event anchored on *path*, so the state is
    branch-correct. When the turn regenerates *replacing*, the user-made changes
    anchored on that reply ride along as ``state_carried``: the new reply is
    written with the correction, while the discarded reply's model-made changes
    are regenerated rather than copied.
    """
    ctx.director["fragment_state"] = await db.fold_path_state(conversation_id, [m["id"] for m in path])
    carried = await db.get_state_events_for_message(replacing["id"], sources={"user"}) if replacing else []
    ctx.director["state_carried"] = [dict(event) for event in carried]


async def _resolve_target_and_parent(
    conversation_id: str, assistant_msg_id: int
) -> tuple[Mapping[str, Any], Mapping[str, Any]] | str:
    """Load an assistant message and its parent message.

    Returns ``(target, user_msg)`` on success, or an error string if the
    message is missing, belongs to a different conversation, or is not an
    assistant message.

    The three cases get three sentences: a client can hold a target id that the
    view it was painted from no longer has (another tab deleted the message),
    and a single opaque "invalid" leaves the reader unable to tell a stale id
    from a mis-wired button.
    """
    target = await db.get_message_by_id(assistant_msg_id)
    if not target or target["conversation_id"] != conversation_id:
        return "That message is no longer in this conversation — reload it"
    if target["role"] != "assistant":
        return "Only an assistant reply can be regenerated"
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

    Resets the director's active moods and state fragments to the pre-turn
    baseline so the regenerated reply starts from the same state as the original,
    plus the user's corrections anchored on it. Returns ``(history, attachments)``.
    """
    if parent_msg.get("role") == "user":
        history_parent_id: int | None = parent_msg.get("parent_id")
    else:
        history_parent_id = parent_msg.get("id")
    history = await db.get_path_to_leaf(conversation_id, history_parent_id) if history_parent_id is not None else []
    moods_before = await db.get_moods_before_turn(conversation_id, target["turn_index"] - 1)
    ctx.director["active_moods"] = moods_before
    ctx.director["fragment_cooldowns"] = cooldown.branch_baseline(history)
    ctx.director["decision_cooldowns"] = decision_cooldown_baseline(
        history, before_exchange_id=str(target.get("exchange_id") or "") or None
    )
    ctx.director["decision_replay"] = stored_evaluations(db.decision_evaluations_of(target))
    # State folds through the parent even when *history* stops short of it: a
    # correction the user anchored on the parent user message (the leaf before
    # this reply existed) is on this reply's path, exactly as on the first turn.
    state_path = [*history, parent_msg] if parent_msg.get("role") == "user" else history
    await _load_fragment_state(ctx, conversation_id, state_path, replacing=target)
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
    decision_input: tuple[Sequence[Mapping[str, Any]], str] | None = None,
) -> AsyncIterator[dict]:
    """Run setup → pipeline → persist and stream all SSE events.

    The user message row must already be persisted before this is called.

    *user_message* is what the writer actually receives; it may differ from
    *last_user_message* (the steered paths send an OOC message as the writer
    input while *last_user_message* carries the original).

    *decision_input* is the ``(history, current request)`` the judge reads when
    it must differ from the writer's, as on the steered paths; ``None`` means
    the writer's own *history* and *user_message*.
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

    decision_history, decision_request = decision_input or (history, user_message)
    judge: JudgeResult | None = None
    async for ev in staged(
        STAGE_JUDGE,
        _run_judge(
            ctx,
            history=decision_history,
            current_request=decision_request,
            macros=setup.macros,
            anchor_message_id=(
                user_msg_id if user_msg_id is not None else (decision_history[-1]["id"] if decision_history else None)
            ),
        ),
    ):
        if isinstance(ev, JudgeResult):
            judge = ev
        else:
            yield ev
    # The same guard the group driver has. Cancellation is not a provider
    # failure: the director pass already refuses to call once the token is set,
    # but without this the turn still announces a directing phase it will not run.
    if ctx.client.is_aborted:
        yield {"event": "done"}
        return

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
        judge=judge,
        state_contract=ctx.state_contract,
    )
    async for event in _consume_pipeline(
        pipeline,
        conversation_id,
        settings,
        user_msg_id,
        asst_turn_index,
        extra_on_result=_conversation_log_writer(conversation_id, log_turn_index),
        world_source_user_msg_id=user_msg_id,
    ):
        yield event


async def _generate_group_exchange(
    ctx: PipelineContext,
    conversation_id: str,
    *,
    history: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Sequence[Mapping[str, Any]],
    parent_message_id: int | None,
    first_turn_index: int,
    exchange_id: str,
    pinned_speaker_id: str | None,
    append_user_to_history: bool = True,
    source_user_message_id: int | None = None,
    editor_audit_msgs: list[str] | None = None,
    decision_exchange_id: str | None = None,
    decision_steering: str = "",
) -> AsyncIterator[dict]:
    """Run one shared Director setup followed by zero or more speaker pipelines."""
    settings = ctx.settings
    rows = list(ctx.group_members)
    eligible = [m for m in rows if not m.get("muted")]
    if error := _group_pin_error(ctx, pinned_speaker_id):
        yield {"event": "error", "data": error}
        return

    # `manual` with nobody picked is the scene resting, which is the same empty
    # plan the Director may choose in `director` mode — the user's message has
    # landed and no one answers it yet. It exits here rather than falling
    # through to plan resolution because a rest that has already been decided
    # must not cost a Director call, and `_prepare_turn` would run one.
    if not pinned_speaker_id and ctx.conv.get("group_turn_mode") == "manual":
        yield {"event": "speaking_plan", "data": {"exchange_id": exchange_id, "plan": []}}
        yield {"event": "done"}
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
    shared = open_turn_state(ctx.director, setup.macros.resolve_message(user_message))

    judge = _committed_exchange_decisions(history, parent_message_id, decision_exchange_id)
    if judge is not None:
        if judge.evaluations or judge.skipped:
            yield {"event": "decisions", "data": judge.as_event_data()}
    else:
        decision_history, decision_request, decision_anchor = _exchange_decision_input(
            history, user_message, parent_message_id, exchange_id=decision_exchange_id, steering=decision_steering
        )
        async for ev in staged(
            STAGE_JUDGE,
            _run_judge(
                ctx,
                history=decision_history,
                current_request=decision_request,
                macros=setup.macros,
                anchor_message_id=decision_anchor,
            ),
        ):
            if isinstance(ev, JudgeResult):
                judge = ev
            else:
                yield ev
    if judge is not None:
        judge.apply_to(shared)
    if ctx.client.is_aborted:
        yield {"event": "done"}
        return

    # One Director stage for the whole exchange, including its before-Writer state
    # changes: they ride the exchange's first reply, the row `_consume_pipeline`
    # anchors them to, and every speaker writes with the resulting state.
    async for ev in run_director_stage(
        cfg,
        shared,
        settings=settings,
        director=ctx.director,
        mood_fragments=ctx.mood_fragments,
        interactive_fragments=ctx.interactive_fragments,
        state_contract=ctx.state_contract,
        attachments=attachments,
        kv_tracker=setup.kv_tracker,
        lorebook=setup.lorebook,
        macros=setup.macros,
        # The castable roster rides the Director's request, not the shared tool
        # blob: mute is otherwise prefix-neutral, and a schema that named the cast
        # turned every mute toggle into a full re-prefill (kv-cache.md, Invariant 3).
        # Same list the plan is validated against below, so the request cannot
        # advertise a key `parse_speaking_plan` would then reject.
        speaker_keys=", ".join(str(member["speaker_key"]) for member in eligible),
    ):
        yield ev
    if ctx.client.is_aborted:
        yield {"event": "done"}
        return

    # Who speaks is settled here; what the Director wrote for whoever that turns
    # out to be is settled by `plan_cue`. The two are separate questions -- a pin
    # and round-robin answer the first without the plan and still deserve the
    # second, or the Director is half-ignored on every path but `director`.
    plan_rows: list[tuple[Mapping[str, Any], str]]
    raw_plan = shared.extra_fields.get("speaking_plan")
    if pinned_speaker_id:
        pinned = next(m for m in eligible if m["id"] == pinned_speaker_id)
        plan_rows = [(pinned, plan_cue(raw_plan, rows, pinned_speaker_id))]
    elif ctx.conv.get("group_turn_mode") == "round_robin":
        member = round_robin_member(rows, history)
        plan_rows = [(member, plan_cue(raw_plan, rows, str(member["id"])))] if member else []
    else:
        parsed = parse_speaking_plan(raw_plan, rows, int(ctx.conv["group_max_speakers"]))
        if parsed is None:
            # Unusable plan, not merely an unused one: nothing in it resolved to a
            # member, so there is no cue to carry over to the fallback speaker.
            member = round_robin_member(rows, history)
            plan_rows = [(member, "")] if member else []
        else:
            plan_rows = parsed

    cast_by_id = {member.member_id: member for member in ctx.cast.members}
    # Only when this request brought no user message of its own: a `/send` starts a
    # new round, so looking back would drag the previous one into this one's evidence.
    # Named through `ctx.speaker_names`, which covers members the roster has since
    # dropped -- their lines are still part of the round the sheet pass reads.
    prior_user, prior_lines = ("", []) if user_message else _round_prefix(history, ctx.speaker_names)
    public_plan = [
        {
            "member_id": row["id"],
            "card_id": row.get("character_card_id"),
            "name": row["display_name"],
            "cue": cue,
        }
        for row, cue in plan_rows
    ]
    yield {"event": "speaking_plan", "data": {"exchange_id": exchange_id, "plan": public_plan}}

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
                "exchange_id": exchange_id,
                # Only the first speaker receives the uploads as its own trailing
                # attachments; every later one has to read them off this row, or
                # an exchange would answer an image only one member ever saw.
                "user_attachments": _history_attachments(attachments),
            }
        )

    # Under Classic card swap the shared setup base is the *neutral* one — the
    # Director ran before a speaker was known — so even the first speaker has to
    # rebuild its prefix around its own card. Reusing the setup base there is a
    # correctness bug in that mode, not merely a cache miss.
    speaker_scoped = prefix_is_speaker_scoped(ctx.cast.context_mode)
    current_parent = parent_message_id
    # What the exchange has actually said so far, in order: (member id, name, reply).
    # The sheet stage is gated on this rather than on the plan -- a planned
    # speaker that never persisted a reply left no prose, so there is nothing
    # about it to record and nothing to bill a call for.
    spoke: list[tuple[str, str, str]] = []
    for index, (row, speaker_cue) in enumerate(plan_rows):
        if ctx.client.is_aborted:
            break
        speaker = cast_by_id.get(str(row["id"]))
        if speaker is None:
            break
        # The exchange's last planned speaker carries every once-per-exchange step:
        # the Dynamic Worlds proposal, the sheet pass, and the after-reply state step.
        is_final = index == len(plan_rows) - 1
        turn_index = first_turn_index + index
        yield {
            "event": "speaker_start",
            "data": {
                "exchange_id": exchange_id,
                "member_id": speaker.member_id,
                "card_id": speaker.card_id,
                "name": speaker.name,
                "index": index,
                "total": len(plan_rows),
                "cue": speaker_cue,
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
            editor_audit_msgs=editor_audit_msgs,
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
            world_proposal=setup.world_proposal,
            # Once per exchange, on the last speaker: the pass reads the whole exchange,
            # and running it per speaker would bill the same members again for
            # the same scene with one more line in it.
            sheet_update=(
                SheetUpdateTurn(
                    conversation_id=conversation_id,
                    exchange_id=exchange_id,
                    # This request's speakers, not the round's: `spoke` is who to
                    # propose *about*, and an earlier request already billed a call
                    # for the members it ran. Only the evidence below widens.
                    member_ids=(*(mid for mid, _, _ in spoke), speaker.member_id),
                    user_message=user_message or prior_user,
                    speaker_name=speaker.name,
                    lines=(*prior_lines, *((name, text) for _, name, text in spoke)),
                )
                # The mode belongs in this condition and not only in Group settings.
                # Under Shared and Swap a member's sheet is rendered into the *cached*
                # body, so an applied update rebuilds the whole scene prefix — the exact
                # cost the opt-in is gated on avoiding. Leaving that invariant to one
                # line of the client meant a `PUT` that changed only the mode left the
                # pass running against the layout it was never priced for.
                if is_final and ctx.conv.get("group_sheet_updates") and tail_carries_identity(ctx.cast.context_mode)
                else None
            ),
            speaker=speaker,
            speaker_cue=speaker_cue,
            context_mode=ctx.cast.context_mode,
            run_director=False,
            director_seed=shared,
            run_exchange_final=is_final,
            state_contract=ctx.state_contract,
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
            exchange_id=exchange_id,
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
        spoke.append((speaker.member_id, speaker.name, persisted_content))
        # The exchange's before-Writer state changes have now landed on its first
        # reply. They are one update, not one per speaker, so the seed stops
        # carrying them before the next speaker copies (and re-persists) the same
        # rows; the resulting state view stays, so every speaker reads it.
        shared.state_events = []
        shared.state_report = empty_state_report()
        grown_history.append(
            {
                "id": persisted_id,
                "role": "assistant",
                "content": persisted_content,
                "turn_index": turn_index,
                "parent_id": current_parent,
                "speaker_member_id": speaker.member_id,
                "exchange_id": exchange_id,
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

        # Branch-aware state, unlike conversation_logs: folded from the active path.
        ctx.director["fragment_cooldowns"] = cooldown.branch_baseline(messages)
        ctx.director["decision_cooldowns"] = decision_cooldown_baseline(messages)
        await _load_fragment_state(ctx, conversation_id, messages)

        exchange_id: str | None = None
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
            exchange_id = str(uuid.uuid4()) if ctx.cast.grouped else None
            user_msg_id, _ = await db.add_message(
                conversation_id,
                "user",
                user_message,
                next_turn,
                parent_id=user_parent_id,
                attachments=db_attachments,
                exchange_id=exchange_id,
                advance_leaf=True,
            )
            # content carries the macro-resolved text so the frontend can sync
            # the optimistic bubble with what was actually persisted.
            yield {"event": "user_message_created", "data": {"id": user_msg_id, "content": user_message}}

        asst_turn = user_turn + 1

        if ctx.cast.grouped:
            if skip_user_persist:
                exchange_id = str(messages[-1].get("exchange_id") or uuid.uuid4())
            assert exchange_id is not None
            async for event in _generate_group_exchange(
                ctx,
                conversation_id,
                history=history,
                user_message=user_message,
                attachments=attachments,
                parent_message_id=user_msg_id,
                first_turn_index=asst_turn,
                exchange_id=exchange_id,
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
    """Generate a pinned group exchange without inserting a synthetic user row."""

    async def _body(ctx: PipelineContext) -> AsyncIterator[dict]:
        if not ctx.cast.grouped:
            yield {"event": "error", "data": "Conversation is not a group"}
            return
        messages = await db.get_messages(conversation_id)
        await _load_fragment_state(ctx, conversation_id, messages)
        ctx.director["fragment_cooldowns"] = cooldown.branch_baseline(messages)
        ctx.director["decision_cooldowns"] = decision_cooldown_baseline(messages)
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0
        async for event in _generate_group_exchange(
            ctx,
            conversation_id,
            history=messages,
            user_message="",
            attachments=[],
            parent_message_id=ctx.conv.get("active_leaf_id"),
            first_turn_index=next_turn,
            exchange_id=str(uuid.uuid4()),
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
        if not original or original["conversation_id"] != conversation_id:
            yield {"event": "error", "data": "That message is no longer in this conversation — reload it"}
            return
        if original["role"] != "user":
            yield {"event": "error", "data": "Only a user message can be edited into a fork"}
            return

        parent_id: int | None = original["parent_id"]
        turn_index = original["turn_index"]
        asst_turn = turn_index + 1
        history = await db.get_path_to_leaf(conversation_id, parent_id) if parent_id is not None else []

        # Reset director to the branch-point baseline (branch-aware state).
        ctx.director["active_moods"] = await db.get_moods_before_turn(conversation_id, turn_index)
        ctx.director["fragment_cooldowns"] = cooldown.branch_baseline(history)
        ctx.director["decision_cooldowns"] = decision_cooldown_baseline(history)
        await _load_fragment_state(ctx, conversation_id, history)

        # Carry original attachments onto the new sibling.
        carried_atts = await db.get_user_attachments_for_message(user_msg_id)

        exchange_id = str(uuid.uuid4()) if ctx.cast.grouped else None
        new_user_id, _ = await db.add_message(
            conversation_id,
            "user",
            new_content,
            turn_index,
            parent_id=parent_id,
            attachments=carried_atts,
            exchange_id=exchange_id,
            advance_leaf=True,
        )
        yield {"event": "user_message_created", "data": {"id": new_user_id, "content": new_content}}

        if ctx.cast.grouped:
            assert exchange_id is not None
            async for event in _generate_group_exchange(
                ctx,
                conversation_id,
                history=history,
                user_message=new_content,
                attachments=carried_atts,
                parent_message_id=new_user_id,
                first_turn_index=asst_turn,
                exchange_id=exchange_id,
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
            async for event in _generate_group_exchange(
                ctx,
                conversation_id,
                history=history,
                user_message=current_message,
                attachments=attachments,
                parent_message_id=user_msg_id,
                first_turn_index=target["turn_index"],
                exchange_id=str(target.get("exchange_id") or uuid.uuid4()),
                pinned_speaker_id=str(speaker_id),
                source_user_message_id=source_user_id,
                decision_exchange_id=str(target.get("exchange_id") or "") or None,
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
            async for event in _generate_group_exchange(
                ctx,
                conversation_id,
                history=extended_history,
                user_message=steer_msg,
                attachments=attachments,
                parent_message_id=user_msg_id,
                first_turn_index=target["turn_index"],
                # The target's own exchange, as `handle_regenerate` does: this is a
                # sibling at the same turn index under the same parent, so it
                # occupies the same slot in the exchange it is replacing.
                exchange_id=str(target.get("exchange_id") or uuid.uuid4()),
                pinned_speaker_id=str(speaker_id),
                append_user_to_history=False,
                source_user_message_id=source_user_id,
                editor_audit_msgs=editor_audit_msgs,
                # Judge the original exchange input; include steering separately.
                decision_exchange_id=str(target.get("exchange_id") or "") or None,
                decision_steering=steer_msg,
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
            # Judge the original request plus steering, before the replaced reply.
            decision_input=(history, "\n\n".join(part for part in (user_msg["content"], steer_msg) if part)),
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
