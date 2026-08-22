"""
orchestrator.py — Sequences the three passes for one turn.

:func:`_run_pipeline` resolves the per-turn config, threads a mutable
:class:`TurnState` through the director, writer, and editor stages, runs
POST_PIPELINE workflow hooks, and emits the terminal ``_result`` event.

Context loading lives in ``context``, persistence in ``persistence``, and the
public entry points in ``entrypoints``. ``_run_pipeline`` is called by
``_generate_reply`` (and directly by tests).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any, TypeVar

from ..core import CastMember, ChatMessage, GroupContextMode, Macros
from ..database.models import PhraseGroup
from ..inference import LLMClient, _KVCacheTracker
from .config import _resolve_pipeline_config, _split_interactive_fragments
from .failures import (
    STAGE_DIRECTOR,
    STAGE_EDITOR,
    STAGE_WORKFLOWS,
    STAGE_WRITER,
    mark_stage,
)
from .passes.director import direction_note_step, director_stage
from .passes.editor import editor_stage
from .passes.writer import strip_speaker_label, writer_stage
from .predicates import direction_note_recording_active
from .sheet_update import sheet_update_stage
from .state import LorebookTurn, SheetUpdateTurn, TurnState, WorldProposalTurn
from .workflow_bridge import _PostPipelineResult, _run_post_pipeline
from .world_proposal import world_proposal_stage

logger = logging.getLogger(__name__)

_Ev = TypeVar("_Ev")


# ── Core pipeline ─────────────────────────────────────────────────────────────


async def _staged(stage: str, gen: AsyncIterator[_Ev]) -> AsyncIterator[_Ev]:
    """Pass *gen*'s events through, labelling anything that escapes it with *stage*.

    The sequence below is the only code that knows which pass is running, and a
    failure is classified two layers up in ``entrypoints._run_turn_handler``. So
    the label rides the exception (see ``failures.mark_stage``).

    Worth the wrapper because the alternative was a frontend guess keyed on the
    last phase event, and the phases do not line up with the passes: nothing moves
    the frontend off ``"directing"`` until the writer's first token, so every
    writer *rejection* -- which by definition arrives before any token -- used to
    be reported against the director.

    ``except Exception`` deliberately excludes ``GeneratorExit`` and
    ``CancelledError``: a stop is not a failed stage and must stay untouched.
    """
    try:
        async for ev in gen:
            yield ev
    except Exception as e:
        mark_stage(e, stage)
        raise


def _make_result(state: TurnState, staged: list[dict] | None = None, staged_state: dict | None = None) -> dict:
    """Build the terminal ``_result`` SSE event from *state*.

    *staged* / *staged_state* are workflow attachments and per-message state
    produced by post-pipeline hooks; they are folded onto *state* before
    serialization so the whole result travels as one object.
    """
    state.staged_attachments = staged or []
    state.staged_message_state = staged_state or {}
    return {"event": "_result", "data": state.as_result_event_data()}


async def _consume_direction_note_step(gen: AsyncIterator[dict], state: TurnState, pass_label: str) -> AsyncIterator[dict]:
    """Drain a direction-note step: stream its reasoning under *pass_label*, keep the notes.

    Notes accumulate across the turn's two placements; the event carries the running total so
    the inspector shows every note recorded this turn regardless of which step produced it.
    """
    async for ev in gen:
        if ev["type"] == "reasoning":
            yield {"event": "reasoning", "data": {"pass": pass_label, "delta": ev["delta"]}}
        elif ev["type"] == "done":
            if ev["result"].notes:
                state.direction_notes.extend(ev["result"].notes)
                yield {"event": "direction_notes", "data": {"notes": state.direction_notes}}


async def _run_pipeline(
    client: LLMClient,
    settings: Mapping[str, Any],
    director: Mapping[str, Any],
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    user_message: str,
    attachments: Sequence[Mapping[str, Any]] | None = None,
    phrase_bank: list[PhraseGroup] | None = None,
    editor_audit_msgs: list[str] | None = None,
    agent_client: LLMClient | None = None,
    agent_prefix: list[ChatMessage] | None = None,
    macros: Macros | None = None,
    conversation_id: str | None = None,
    character_id: str | None = None,
    card: Mapping[str, Any] | None = None,
    *,
    prefix: list[ChatMessage],
    enabled_tools: Mapping[str, bool],
    turn_scratch: dict,
    kv_tracker: _KVCacheTracker,
    schema_overrides: Mapping[str, dict],
    history: Sequence[Mapping[str, Any]] | None = None,
    lorebook: LorebookTurn | None = None,
    world_proposal: WorldProposalTurn | None = None,
    sheet_update: SheetUpdateTurn | None = None,
    speaker: CastMember | None = None,
    speaker_cue: str = "",
    context_mode: GroupContextMode = "private",
    run_director: bool = True,
    director_seed: TurnState | None = None,
    run_exchange_final: bool = True,
) -> AsyncIterator[dict]:
    """Run the director → writer → editor passes for one turn.

    Streams SSE events as each pass runs, then runs post-pipeline workflow
    hooks and emits a single ``_result`` event with the final draft and any
    workflow attachments.

    A stop during the director pass exits cleanly with no output. A stop during
    the writer pass still emits ``_result`` with the partial draft so persistence
    can save it.
    """
    if macros is None:
        macros = Macros("User", "")
    if attachments is None:
        attachments = []
    if lorebook is None:
        lorebook = LorebookTurn(entries=(), messages=(), agentic=False)

    user_message = macros.resolve_message(user_message)

    # Resolved once; cfg.enabled_tools is the length-guard-folded map.
    cfg = _resolve_pipeline_config(
        settings,
        enabled_tools,
        macros=macros,
        client=client,
        agent_client=agent_client,
        agent_prefix=agent_prefix,
        prefix=prefix,
        phrase_bank=phrase_bank,
        schema_overrides=schema_overrides,
    )

    # feedback fragments are handled post-writer and direction-note fragments by the
    # direction-note step; the rest shape the writer prompt.
    writer_fragments, feedback_fragments, direction_note_fragments = _split_interactive_fragments(interactive_fragments)

    # Each direction-note fragment chooses its own recording placement, so a turn may run a
    # pre-writer step, a post-turn step, or both. The shared tool blob still carries the union
    # of all of them, keeping the cached prefix byte-stable across both steps.
    pre_writer_notes = [df for df in direction_note_fragments if df.get("direction_note_timing") == "pre_writer"]
    post_turn_notes = [df for df in direction_note_fragments if df.get("direction_note_timing") != "pre_writer"]

    # Mutable state threaded through the three passes; seeded from director + user message.
    # macro_choices is copied so mutations stay turn-local until persistence
    # commits them (regenerates then re-read the committed map, like moods).
    state = TurnState(
        user_message=user_message,
        effective_msg=user_message,
        active_moods=director["active_moods"],
        macro_choices=dict(director.get("macro_choices") or {}),
    )
    # A group exchange runs one Director for every speaker, so speakers 2..n start
    # from its result instead of re-deriving it. Which fields that covers is
    # ``TurnState``'s to say (``_DIRECTOR_SEED_FIELDS``), not this module's --
    # including the pre-writer notes, which the driver clears from the seed once
    # the exchange's first reply has anchored them.
    if director_seed is not None:
        state.seed_from(director_seed)

    # --- Director pass (+ rewrite, style injection, agentic-lorebook block) ---
    if run_director:
        async for ev in _staged(
            STAGE_DIRECTOR,
            director_stage(
                cfg,
                state,
                settings=settings,
                director=director,
                mood_fragments=mood_fragments,
                writer_fragments=writer_fragments,
                attachments=attachments,
                kv_tracker=kv_tracker,
                lorebook=lorebook,
                macros=macros,
            ),
        ):
            yield ev

    # Both clients share one abort token, so checking either is equivalent.
    if client.is_aborted:
        return

    # --- Direction-note step (pre-writer placement) ---
    # Reflects on the scene direction the director just set, so it requires
    # direct_scene (which is what produces that direction).
    if (
        run_director
        and direction_note_recording_active(settings, pre_writer_notes, agent_on=cfg.agent_on)
        and cfg.enabled_tools.get("direct_scene")
    ):
        async for ev in _staged(
            STAGE_DIRECTOR,
            _consume_direction_note_step(
                direction_note_step(
                    cfg.agent_lane.client,
                    cfg.agent_lane.base,
                    settings=settings,
                    direction_note_fragments=pre_writer_notes,
                    active_notes=director.get("direction_notes") or [],
                    placement="pre_writer",
                    inj_block=state.scene_direction,
                    kv_tracker=kv_tracker,
                    reasoning_on=cfg.director_reasoning_on,
                    reasoning_prefill=cfg.director_reasoning_prefill,
                ),
                state,
                "director",
            ),
        ):
            yield ev

    # --- Writer pass ---
    async for ev in _staged(
        STAGE_WRITER,
        writer_stage(
            cfg,
            state,
            settings=settings,
            attachments=attachments,
            kv_tracker=kv_tracker,
            depth_block=lorebook.depth_block,
            speaker=speaker,
            speaker_cue=speaker_cue,
            macros=macros,
            context_mode=context_mode,
        ),
    ):
        yield ev

    # Aborted mid-writer: persist partial output and skip remaining passes.
    if client.is_aborted:
        yield _make_result(state)
        kv_tracker.log_summary()
        return

    # --- Editor pass (edit loop + post-writer feedback step) ---
    async for ev in _staged(
        STAGE_EDITOR,
        editor_stage(
            cfg,
            state,
            settings=settings,
            phrase_bank=phrase_bank,
            feedback_fragments=feedback_fragments,
            editor_audit_msgs=editor_audit_msgs,
            kv_tracker=kv_tracker,
        ),
    ):
        yield ev

    # A full editor rewrite can reintroduce the model's self-label after the
    # writer's streaming gate removed it. Apply the same pure transform and
    # announce the corrected authoritative draft before workflows consume it.
    if speaker is not None:
        stripped_draft = strip_speaker_label(state.resp_text, speaker.name)
        if stripped_draft != state.resp_text:
            state.resp_text = stripped_draft
            yield {"event": "writer_rewrite", "data": {"refined_text": stripped_draft}}

    # --- Post-pipeline workflow iteration ---
    # director_output is a plain dict (PostCtx expects a read-only mapping).
    director_output = state.as_director_output()
    post: _PostPipelineResult | None = None
    async for ev in _staged(
        STAGE_WORKFLOWS,
        _run_post_pipeline(
            draft=state.resp_text,
            conversation_id=conversation_id,
            character_id=character_id,
            card=card,
            history=history,
            effective_msg=state.effective_msg,
            director_output=director_output,
            settings=settings,
            prefix=prefix,
            enabled_tools=cfg.enabled_tools,
            turn_scratch=turn_scratch,
            client=client,
            kv_tracker=kv_tracker,
            schema_overrides=schema_overrides,
        ),
    ):
        if isinstance(ev, _PostPipelineResult):
            post = ev
        else:
            yield ev
    assert post is not None

    # Fold any hook-rewritten draft back into state before emitting _result.
    state.resp_text = post.draft

    # --- Direction-note step (post-turn placement) ---
    # Sees the finished reply. Skipped on an empty draft (no message to anchor notes
    # to) and on a stop arriving after the last pre-editor abort check.
    if (
        run_exchange_final
        and direction_note_recording_active(settings, post_turn_notes, agent_on=cfg.agent_on)
        and state.resp_text.strip()
        and not client.is_aborted
    ):
        async for ev in _staged(
            STAGE_EDITOR,
            _consume_direction_note_step(
                direction_note_step(
                    cfg.agent_lane.client,
                    cfg.agent_lane.base,
                    settings=settings,
                    direction_note_fragments=post_turn_notes,
                    active_notes=director.get("direction_notes") or [],
                    placement="post_turn",
                    reply_text=state.resp_text,
                    writer_user_msg=state.writer_content,
                    kv_tracker=kv_tracker,
                    reasoning_on=cfg.editor_reasoning_on,
                    reasoning_prefill=cfg.editor_reasoning_prefill,
                ),
                state,
                "editor",
            ),
        ):
            yield ev

    # --- Dynamic Worlds proposal step ---
    # Last, deliberately: it judges the prose that will actually be persisted, so
    # it has to sit after the editor and after any draft-rewriting post-pipeline
    # hook. Same skip conditions as the post-turn notes step -- an empty draft has
    # nothing to derive world state from, and a stop must not start a fresh call.
    if run_exchange_final and world_proposal is not None and state.resp_text.strip() and not client.is_aborted:
        async for ev in _staged(
            STAGE_EDITOR,
            world_proposal_stage(
                cfg,
                state,
                settings=settings,
                turn=world_proposal,
                kv_tracker=kv_tracker,
            ),
        ):
            yield ev

    # --- Scene-local sheet update step ---
    # Last of the post-turn steps, and for the same reason the world stage is
    # second-to-last: it judges the prose that will actually be persisted, so it
    # has to sit after the editor and after any draft-rewriting post-pipeline
    # hook. `run_exchange_final` is what makes it once-per-exchange rather than
    # once-per-speaker; the driver only builds a turn for the final speaker, and
    # the gate here is what keeps that true if another caller forgets.
    if run_exchange_final and sheet_update is not None and state.resp_text.strip() and not client.is_aborted:
        async for ev in _staged(
            STAGE_EDITOR,
            sheet_update_stage(cfg, state, turn=sheet_update),
        ):
            yield ev

    yield _make_result(state, post.staged_attachments, post.staged_message_state)
    kv_tracker.log_summary()
