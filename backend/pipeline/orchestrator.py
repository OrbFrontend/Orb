"""Sequence the Director, Writer, Editor, and post-pipeline stages."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..core import (
    CardScripts,
    CastMember,
    ChatMessage,
    GroupContextMode,
    Macros,
    StateView,
    carry_events,
)
from ..database.models import PhraseGroup
from ..inference import LLMClient, _KVCacheTracker
from .config import _resolve_pipeline_config, _split_interactive_fragments
from .failures import (
    STAGE_DIRECTOR,
    STAGE_EDITOR,
    STAGE_WORKFLOWS,
    STAGE_WRITER,
    mark_stage,
    staged,
)
from .passes.director import (
    apply_state_step_result,
    cooldown,
    director_stage,
    state_event_payload,
)
from .passes.editor import editor_stage
from .passes.judge import JudgeResult
from .passes.state import StateContract, StateStepResult, state_step
from .passes.writer import strip_speaker_label, writer_stage
from .sheet_update import sheet_update_stage
from .state import (
    BranchBaseline,
    LorebookTurn,
    SheetUpdateTurn,
    TurnState,
    WorldProposalTurn,
    _PipelineConfig,
)
from .workflow_bridge import _PostPipelineResult, _run_post_pipeline
from .world_proposal import world_proposal_stage

logger = logging.getLogger(__name__)


def _make_result(state: TurnState) -> dict:
    """Build the terminal ``_result`` SSE event from *state*."""
    return {"event": "_result", "data": state.as_result_event_data()}


def seed_fragment_state(state: TurnState, director: BranchBaseline) -> None:
    """Start *state* from the branch's folded state plus any carried corrections.

    ``director["fragment_state"]`` is the parent path's fold. A regeneration also
    brings the user-made changes anchored on the reply it replaces
    (``director["state_carried"]``): they apply first, so the new reply is
    written with the correction, and they commit ahead of the turn's own changes
    so row order matches apply order. One whose entry the discarded reply added
    has nothing to apply to and is reported as dropped.
    """
    base = director.get("fragment_state")
    view = base.copy() if isinstance(base, StateView) else StateView()
    applied, dropped = carry_events(director.get("state_carried") or (), view)
    state.state_view = view
    state.state_prior = view.copy()
    state.state_events = applied
    state.state_report = {"rejected": [], "dropped": dropped}


def open_turn_state(director: BranchBaseline, user_message: str) -> TurnState:
    """Start a turn's working state from the branch baseline in *director*.

    *user_message* is already macro-resolved. ``macro_choices`` is copied so
    mutations stay turn-local until persistence commits them (regenerates then
    re-read the committed map, like moods).
    """
    state = TurnState(
        user_message=user_message,
        effective_msg=user_message,
        active_moods=director["active_moods"],
        macro_choices=dict(director.get("macro_choices") or {}),
        fragment_cooldowns=dict(director.get("fragment_cooldowns") or {}),
    )
    seed_fragment_state(state, director)
    return state


async def run_director_stage(
    cfg: _PipelineConfig,
    state: TurnState,
    *,
    settings: Mapping[str, Any],
    director: BranchBaseline,
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    state_contract: StateContract,
    attachments: Sequence[Mapping[str, Any]],
    kv_tracker: _KVCacheTracker,
    lorebook: LorebookTurn,
    macros: Macros,
    speaker_keys: str = "",
) -> AsyncIterator[dict]:
    """Announce the state changes *state* was seeded with, then direct the turn.

    The one Director entry for a solo turn and for a group exchange's shared
    Director, so both label a failure as the Director's and read the Judge's
    guidance from *state*.
    """
    try:
        if state.state_events or state.state_report["dropped"]:
            yield {"event": "state", "data": state_event_payload(state)}
        scene_fragments, _, _, _ = _split_interactive_fragments(interactive_fragments)
        async for ev in director_stage(
            cfg,
            state,
            settings=settings,
            director=director,
            mood_fragments=mood_fragments,
            scene_fragments=scene_fragments,
            direct_scene_fragments=state_contract.direct_scene_rows(interactive_fragments),
            state_contract=state_contract,
            attachments=attachments,
            kv_tracker=kv_tracker,
            lorebook=lorebook,
            macros=macros,
            speaker_keys=speaker_keys,
            decision_guidance=state.decision_guidance,
        ):
            yield ev
    except Exception as exc:
        mark_stage(exc, STAGE_DIRECTOR)
        raise


async def _run_pipeline(
    client: LLMClient,
    settings: Mapping[str, Any],
    director: BranchBaseline,
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
    judge: JudgeResult | None = None,
    run_exchange_final: bool = True,
    state_contract: StateContract | None = None,
) -> AsyncIterator[dict]:
    """Run the director → writer → editor passes for one turn.

    Streams SSE events as each pass runs, retains the post-Editor draft, then
    runs the local prose rewriter and post-pipeline workflow hooks before
    emitting one ``_result`` event.

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
    if card and speaker is None:
        user_message = CardScripts.from_extensions(card.get("extensions")).apply(
            user_message, "prompt", "user", macros.resolve_message
        )

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

    # Feedback and post-processing fragments are handled after the Writer, and
    # state fragments by their own routing; scene fragments shape the Writer prompt.
    _, feedback_fragments, state_fragments, post_processing_fragments = _split_interactive_fragments(interactive_fragments)
    # Captured once for the turn: tool construction, routing, validation and
    # commit all read this contract, never the live fragment settings.
    contract = state_contract or StateContract.capture(settings, state_fragments)

    # Mutable state threaded through the three passes.
    state = open_turn_state(director, user_message)
    # Resolved before this call, by the stage that owns the frozen snapshot. The
    # records ride the TurnState so persistence commits them in the same INSERT as
    # the reply they produced; a group exchange's shared result reaches later
    # speakers through ``director_seed`` instead, and is not re-resolved.
    if judge is not None:
        judge.apply_to(state)
    # A group exchange runs one Director for every speaker, so speakers 2..n start
    # from its result instead of re-deriving it. Which fields that covers is
    # ``TurnState``'s to say (``_DIRECTOR_SEED_FIELDS``), not this module's --
    # including the before-Writer state changes, which the driver clears from the
    # seed once the exchange's first reply has anchored them.
    if director_seed is not None:
        state.seed_from(director_seed)

    if run_director:
        async for ev in run_director_stage(
            cfg,
            state,
            settings=settings,
            director=director,
            mood_fragments=mood_fragments,
            interactive_fragments=interactive_fragments,
            state_contract=contract,
            attachments=attachments,
            kv_tracker=kv_tracker,
            lorebook=lorebook,
            macros=macros,
        ):
            yield ev

    # Both clients share one abort token, so checking either is equivalent.
    if client.is_aborted:
        return

    # The live working state, for the fallback save: a turn that fails or is
    # cancelled before ``_result`` still commits what its billed calls produced
    # -- the Director record, decisions, cooldowns, the before-Writer state
    # changes, and the latest authoritative draft. Internal, like ``_result``:
    # consumed by persistence and never sent to the browser.
    yield {"event": "_turn_state", "data": state}

    async for ev in staged(
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

    async for ev in staged(
        STAGE_EDITOR,
        editor_stage(
            cfg,
            state,
            settings=settings,
            phrase_bank=phrase_bank,
            feedback_fragments=feedback_fragments,
            post_processing_fragments=post_processing_fragments,
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

    # Retain the Editor's result before any secondary workflow changes it. The
    # database write still happens atomically with the final assistant message;
    # this snapshot is the source an on-demand prose rewrite can replay later.
    state.writer_draft = state.resp_text

    # A stop during an Editor sub-step keeps the latest authoritative draft but
    # must not start Feedback-adjacent work or any secondary workflow. This is
    # the post-Writer counterpart to the abort boundary above.
    if client.is_aborted:
        yield _make_result(state)
        kv_tracker.log_summary()
        return

    # director_output is a plain dict (PostCtx expects a read-only mapping).
    director_output = state.as_director_output()
    post: _PostPipelineResult | None = None
    async for ev in staged(
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
            # One source for both modes: cfg.agent_lane IS the writer lane when a
            # single model serves both, so a hook's forced Agent call lands on
            # the configured execution target.
            agent_client=cfg.agent_lane.client,
            agent_model_name=cfg.agent_lane.base.model,
        ),
    ):
        if isinstance(ev, _PostPipelineResult):
            post = ev
        else:
            yield ev
    assert post is not None

    # Fold the hooks' output into state at once, so a later failure still saves
    # the rewritten draft and the attachments a hook already paid to render.
    state.resp_text = post.draft
    state.staged_attachments = post.staged_attachments
    state.staged_message_state = post.staged_message_state

    # Sees the finished reply. Skipped on an empty draft (no message to anchor the
    # changes to) and on a stop arriving after the last pre-editor abort check.
    resting = cooldown.blocked(director.get("fragment_cooldowns") or {})
    after_reply = [fragment for fragment in contract.after_reply() if fragment.id not in resting]
    if run_exchange_final and after_reply and state.resp_text.strip() and not client.is_aborted:
        yield {"event": "step_start", "data": {"step": "state"}}
        async for ev in staged(
            STAGE_EDITOR,
            state_step(
                cfg.agent_lane.client,
                cfg.agent_lane.base,
                settings=settings,
                fragments=after_reply,
                view=state.state_view,
                placement="after_reply",
                known_ids=frozenset(fragment.id for fragment in contract.tool_fragments()),
                # No decision guidance: it directs the Writer, rides the replayed
                # Writer message already, and the reply is the only record now.
                reply_text=state.resp_text,
                writer_user_msg=state.writer_content,
                kv_tracker=kv_tracker,
                reasoning_on=cfg.editor_reasoning_on,
                reasoning_prefill=cfg.editor_reasoning_prefill,
            ),
        ):
            if ev["type"] == "reasoning":
                yield {"event": "reasoning", "data": {"pass": "editor", "delta": state.add_reasoning("editor", ev)}}
            elif ev["type"] == "done":
                step_result: StateStepResult = ev["result"]
                apply_state_step_result(state, step_result, contract)
                if step_result.events or step_result.rejections:
                    yield {"event": "state", "data": state_event_payload(state)}

    # Last, deliberately: it judges the prose that will actually be persisted, so
    # it has to sit after the editor and after any draft-rewriting post-pipeline
    # hook. Same skip conditions as the after-reply state step -- an empty draft has
    # nothing to derive world state from, and a stop must not start a fresh call.
    if run_exchange_final and world_proposal is not None and state.resp_text.strip() and not client.is_aborted:
        async for ev in staged(
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

    # Last of the post-turn steps, and for the same reason the world stage is
    # second-to-last: it judges the prose that will actually be persisted, so it
    # has to sit after the editor and after any draft-rewriting post-pipeline
    # hook. `run_exchange_final` is what makes it once-per-exchange rather than
    # once-per-speaker; the driver only builds a turn for the final speaker, and
    # the gate here is what keeps that true if another caller forgets.
    if run_exchange_final and sheet_update is not None and state.resp_text.strip() and not client.is_aborted:
        async for ev in staged(
            STAGE_EDITOR,
            sheet_update_stage(cfg, state, settings=settings, turn=sheet_update),
        ):
            yield ev

    yield _make_result(state)
    kv_tracker.log_summary()
