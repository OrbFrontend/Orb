"""Sequence the Director, Writer, Editor, and post-pipeline stages."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

from ..core import CardScripts, CastMember, ChatMessage, GroupContextMode, Macros, StateFragment, StateView, carry_events
from ..core.settings import Settings
from ..database.models import PhraseGroup
from ..inference import KVCacheTracker, LLMClient
from .config import resolve_pipeline_config, split_interactive_fragments
from .events import CoreTurnEvent, PipelineEvent, PublicTurnEvent, ResultEvent
from .failures import (
    STAGE_AFTER_REPLY,
    STAGE_DIRECTOR,
    STAGE_EDITOR,
    STAGE_WORKFLOWS,
    STAGE_WRITER,
    mark_stage,
    staged,
    step_failure_warning,
)
from .passes.director import apply_state_step_result, cooldown, director_stage, state_event_payload
from .passes.editor import editor_stage
from .passes.judge import JudgeConfig, JudgeResult
from .passes.state import StateContract, StateStepResult, offered_state_ids, state_step
from .passes.writer import strip_speaker_label, writer_stage
from .sheet_update import sheet_update_stage
from .state import BranchBaseline, LorebookTurn, PipelineConfig, SheetUpdateTurn, TurnState, WorldProposalTurn
from .workflow_bridge import PostPipelineResult, run_post_pipeline
from .world_proposal import world_proposal_stage

logger = logging.getLogger(__name__)


def _make_result(state: TurnState) -> ResultEvent:
    """Build the terminal ``_result`` SSE event from *state*."""
    return {"event": "_result", "data": state.as_result_event_data()}


def seed_fragment_state(state: TurnState, director: BranchBaseline) -> None:
    """Start *state* from the branch's folded state plus any carried corrections.

    ``director["fragment_state"]`` is the parent path's fold. A regeneration also brings the user-made changes anchored on the
    reply it replaces (``director["state_carried"]``): they apply first, so the new reply is written with the correction, and
    they commit ahead of the turn's own changes so row order matches apply order. One whose entry the discarded reply added has
    nothing to apply to and is reported as dropped.
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

    *user_message* is already macro-resolved. ``macro_choices`` is copied so mutations stay turn-local until persistence commits
    them (regenerates then re-read the committed map, like moods).
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
    cfg: PipelineConfig,
    state: TurnState,
    *,
    settings: Settings,
    director: BranchBaseline,
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    state_contract: StateContract,
    attachments: Sequence[Mapping[str, Any]],
    kv_tracker: KVCacheTracker,
    lorebook: LorebookTurn,
    macros: Macros,
    speaker_keys: str = "",
) -> AsyncIterator[CoreTurnEvent]:
    """Announce the state changes *state* was seeded with, then direct the turn.

    The one Director entry for a solo turn and for a group exchange's shared Director, so both label a failure as the Director's
    and read the Judge's guidance from *state*.
    """
    try:
        if state.state_events or state.state_report["dropped"]:
            yield {"event": "state", "data": state_event_payload(state)}
        scene_fragments, _, _, _ = split_interactive_fragments(interactive_fragments)
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
            director_decision_guidance=state.director_decision_guidance,
            writer_decision_guidance=state.writer_decision_guidance,
        ):
            yield ev
    except Exception as exc:
        mark_stage(exc, STAGE_DIRECTOR)
        raise


async def run_pipeline(
    client: LLMClient,
    settings: Settings,
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
    kv_tracker: KVCacheTracker,
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
    judge_config: JudgeConfig | None = None,
) -> AsyncIterator[PipelineEvent]:
    """Run the director -> writer -> editor passes for one turn.

    Streams SSE events as each pass runs, retains the post-Editor draft, then runs the local prose rewriter and post-pipeline
    workflow hooks before emitting one ``_result`` event.

    A stop during the director pass exits cleanly with no output. A stop during the writer pass still emits ``_result`` with the
    partial draft so persistence can save it.
    """
    if macros is None:
        macros = Macros("User", "")
    if attachments is None:
        attachments = []
    if lorebook is None:
        lorebook = LorebookTurn(entries=(), agentic=False)

    user_message = _resolve_user_message(user_message, macros, card=card, speaker=speaker)

    # Resolved once; cfg.enabled_tools is the offered blob map, cfg.active_tools the toggles.
    cfg = resolve_pipeline_config(
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
    _, feedback_fragments, state_fragments, post_processing_fragments = split_interactive_fragments(interactive_fragments)
    # Captured once for the turn: tool construction, routing, validation and
    # commit all read this contract, never the live fragment settings.
    contract = state_contract or StateContract.capture(settings, state_fragments)

    # Mutable state threaded through the three passes.
    state = _open_pipeline_state(director, user_message, judge=judge, director_seed=director_seed)

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

    # The live working state, for the fallback save: a turn that fails or is cancelled before ``_result`` still commits what its
    # billed calls produced -- the Director record, decisions, cooldowns, the before-Writer state changes, and the latest
    # authoritative draft. Internal, like ``_result``: consumed by persistence and never sent to the browser.
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
            judge_config=judge_config,
            history=history,
            speaker_member_id=speaker.member_id if speaker is not None else None,
        ),
    ):
        yield ev

    if (relabeled := _restrip_speaker_label(state, speaker)) is not None:
        yield relabeled

    # Retain the Editor's result before any secondary workflow changes it. The database write still happens atomically with the
    # final assistant message; this snapshot is the source an on-demand prose rewrite can replay later.
    state.writer_draft = state.resp_text

    # A stop during an Editor sub-step keeps the latest authoritative draft but must not start Feedback-adjacent work or any
    # secondary workflow. This is the post-Writer counterpart to the abort boundary above.
    if client.is_aborted:
        yield _make_result(state)
        kv_tracker.log_summary()
        return

    async for ev in _workflow_stage(
        cfg,
        state,
        client=client,
        settings=settings,
        prefix=prefix,
        turn_scratch=turn_scratch,
        kv_tracker=kv_tracker,
        schema_overrides=schema_overrides,
        conversation_id=conversation_id,
        character_id=character_id,
        card=card,
        history=history,
    ):
        yield ev

    # `run_exchange_final` is what makes these steps once-per-exchange rather than once-per-speaker; the driver only builds a
    # sheet turn for the final speaker, and the gate here is what keeps that true if another caller forgets.
    if run_exchange_final:
        async for ev in _reply_stages(
            cfg,
            state,
            client=client,
            settings=settings,
            contract=contract,
            director=director,
            kv_tracker=kv_tracker,
            world_proposal=world_proposal,
            sheet_update=sheet_update,
        ):
            yield ev

    yield _make_result(state)
    kv_tracker.log_summary()


def _resolve_user_message(
    user_message: str, macros: Macros, *, card: Mapping[str, Any] | None, speaker: CastMember | None
) -> str:
    """Resolve *user_message*'s macros, then run the card's prompt scripts over it unless a group *speaker* is replying."""
    resolved = macros.resolve_message(user_message)
    if not card or speaker is not None:
        return resolved
    return CardScripts.from_extensions(card.get("extensions")).apply(resolved, "prompt", "user", macros.resolve_message)


def _open_pipeline_state(
    director: BranchBaseline, user_message: str, *, judge: JudgeResult | None, director_seed: TurnState | None
) -> TurnState:
    """Open the turn's working state, with the Judge's records and a shared Director result already folded in."""
    state = open_turn_state(director, user_message)
    # Resolved before this call, by the stage that owns the frozen snapshot. The records ride the TurnState so persistence
    # commits them in the same INSERT as the reply they produced; a group exchange's shared result reaches later speakers
    # through ``director_seed`` instead, and is not re-resolved.
    if judge is not None:
        judge.apply_to(state)
    # A group exchange runs one Director for every speaker, so speakers 2..n start from its result instead of re-deriving it.
    # Which fields that covers is ``TurnState``'s to say (``_DIRECTOR_SEED_FIELDS``), not this module's -- including the
    # before-Writer state changes, which the driver clears from the seed once the exchange's first reply has anchored them.
    if director_seed is not None:
        state.seed_from(director_seed)
    return state


def _restrip_speaker_label(state: TurnState, speaker: CastMember | None) -> CoreTurnEvent | None:
    """Strip a self-label from *speaker*'s Editor draft; the corrected draft's announcement, or None when nothing changed.

    A full editor rewrite can reintroduce the model's self-label after the writer's streaming gate removed it. Apply the same
    pure transform and announce the corrected authoritative draft before workflows consume it.
    """
    if speaker is None:
        return None
    stripped_draft = strip_speaker_label(state.resp_text, speaker.name)
    if stripped_draft == state.resp_text:
        return None
    state.resp_text = stripped_draft
    return {"event": "writer_rewrite", "data": {"refined_text": stripped_draft}}


async def _workflow_stage(
    cfg: PipelineConfig,
    state: TurnState,
    *,
    client: LLMClient,
    settings: Settings,
    prefix: list[ChatMessage],
    turn_scratch: dict,
    kv_tracker: KVCacheTracker,
    schema_overrides: Mapping[str, dict],
    conversation_id: str | None,
    character_id: str | None,
    card: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]] | None,
) -> AsyncIterator[PublicTurnEvent]:
    """Run the post-pipeline workflow hooks over the Editor's draft and fold what they hand back into *state*."""

    def fold_post(result: PostPipelineResult) -> None:
        # Fold the hooks' output into state as each piece is handed over, so a stop or failure mid-hook still saves the
        # rewritten draft and the attachments a hook already paid to render.
        state.resp_text = result.draft
        state.staged_attachments = result.staged_attachments
        state.staged_message_state = result.staged_message_state

    post: PostPipelineResult | None = None
    async for ev in staged(
        STAGE_WORKFLOWS,
        run_post_pipeline(
            draft=state.resp_text,
            conversation_id=conversation_id,
            character_id=character_id,
            card=card,
            history=history,
            effective_msg=state.effective_msg,
            # A plain dict (PostCtx expects a read-only mapping).
            director_output=state.as_director_output(),
            settings=settings,
            prefix=prefix,
            enabled_tools=cfg.enabled_tools,
            turn_scratch=turn_scratch,
            client=client,
            kv_tracker=kv_tracker,
            schema_overrides=schema_overrides,
            # One source for both modes: cfg.agent_lane IS the writer lane when a single model serves both, so a hook's forced
            # Agent call lands on the configured execution target.
            agent_client=cfg.agent_lane.client,
            agent_model_name=cfg.agent_lane.base.model,
            on_accepted=fold_post,
        ),
    ):
        if isinstance(ev, PostPipelineResult):
            post = ev
        else:
            yield ev
    assert post is not None
    fold_post(post)


def _reply_stands(state: TurnState, client: LLMClient) -> bool:
    """Whether a step judging the finished reply may start: there is a reply to anchor on, and no stop has arrived."""
    return bool(state.resp_text.strip()) and not client.is_aborted


async def _reply_stages(
    cfg: PipelineConfig,
    state: TurnState,
    *,
    client: LLMClient,
    settings: Settings,
    contract: StateContract,
    director: BranchBaseline,
    kv_tracker: KVCacheTracker,
    world_proposal: WorldProposalTurn | None,
    sheet_update: SheetUpdateTurn | None,
) -> AsyncIterator[CoreTurnEvent]:
    """The steps that judge the finished reply: after-reply state, then world changes, then sheet updates.

    Last, deliberately: they judge the prose that will actually be persisted, so they sit after the editor and after any
    draft-rewriting post-pipeline hook. Each is skipped on an empty draft (no message to anchor the changes to, nothing to
    derive world state from) and once a stop has arrived, so a stop never starts a fresh call.
    """
    resting = cooldown.blocked(director.get("fragment_cooldowns") or {})
    after_reply = [fragment for fragment in contract.after_reply() if fragment.id not in resting]
    if after_reply and _reply_stands(state, client):
        async for ev in _after_reply_state_stage(
            cfg, state, settings=settings, fragments=after_reply, contract=contract, kv_tracker=kv_tracker
        ):
            yield ev

    if world_proposal is not None and _reply_stands(state, client):
        async for ev in staged(
            STAGE_AFTER_REPLY, world_proposal_stage(cfg, state, settings=settings, turn=world_proposal, kv_tracker=kv_tracker)
        ):
            yield ev

    if sheet_update is not None and _reply_stands(state, client):
        async for ev in staged(STAGE_AFTER_REPLY, sheet_update_stage(cfg, state, settings=settings, turn=sheet_update)):
            yield ev


async def _after_reply_state_stage(
    cfg: PipelineConfig,
    state: TurnState,
    *,
    settings: Settings,
    fragments: Sequence[StateFragment],
    contract: StateContract,
    kv_tracker: KVCacheTracker,
) -> AsyncIterator[CoreTurnEvent]:
    """Let the after-reply state *fragments* read the finished reply, and apply the changes they make."""
    yield {"event": "step_start", "data": {"step": "state"}}
    async for ev in staged(
        STAGE_AFTER_REPLY,
        state_step(
            cfg.agent_lane.client,
            cfg.agent_lane.base,
            settings=settings,
            fragments=fragments,
            view=state.state_view,
            placement="after_reply",
            known_ids=offered_state_ids(cfg.agent_lane.base),
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
        elif ev["type"] == "failure":
            yield step_failure_warning(ev["error"], "state", stage=STAGE_AFTER_REPLY)
        elif ev["type"] == "done":
            step_result: StateStepResult = ev["result"]
            apply_state_step_result(state, step_result, contract)
            if step_result.events or step_result.rejections:
                yield {"event": "state", "data": state_event_payload(state)}
