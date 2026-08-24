"""
state.py — Per-turn dataclasses shared across passes.

``ModelLane``, ``_PipelineConfig``, and ``TurnState`` are built by the
orchestrator and consumed by the director, writer, and editor passes. They live
here so the passes depend downward into ``state`` rather than upward into the
orchestrator.

``TurnState`` travels the full turn: passes mutate it, the orchestrator
serializes a result-subset into the ``_result`` SSE event via
``as_result_event_data``, and persistence rehydrates a fresh ``TurnState`` from
that dict to drive the saves.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core import ChatMessage, ContentPart, Macros
from ..features.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    LOREBOOK_SCAN_DEPTH,
    compute_lorebook_block,
)
from ..inference import CachedBase, LLMClient
from .passes.editor.length_guard import LengthGuard
from .passes.editor.slm_rewrite import ProseRewrite


@dataclass(frozen=True, slots=True)
class ModelLane:
    """One model's call surface for a turn: an LLM client paired with its
    cached base (prefix + tool blob + model name + macro resolver).

    A turn has two lanes — ``writer`` and ``agent`` (director + editor). In
    single-model mode both lanes are the same object, making the KV-cache
    byte-identity invariant structural rather than a per-call-site convention.
    In dual-model mode the agent lane carries its own client and prefix, while
    the writer lane has an empty tool blob (Invariant 5).

    Reasoning is per-pass (director and editor share the agent lane but toggle
    reasoning independently), so it is not part of the lane.
    """

    client: LLMClient
    base: CachedBase

    def sends_tool_schemas(self, trailing: Sequence[ChatMessage], *, tools_in_prompt: bool = True) -> bool:
        """Whether a call extending this lane sends its frozen schema tuple.

        The base owns schema presence; the client owns transport/profile policy.
        Keeping the conjunction here gives every pass the same answer and leaves
        room for call-dependent transports such as text mode's multimodal chat
        fallback without duplicating that knowledge in pipeline configuration.
        """
        if not self.base.tools:
            return False
        messages = [*self.base.prefix, *trailing]
        return self.client.sends_tool_schemas(messages, self.base.model, tools_in_prompt=tools_in_prompt)


@dataclass(slots=True)
class _PipelineConfig:
    """Resolved per-turn flags, lanes, and prefixes for ``_run_pipeline``."""

    agent_on: bool
    enabled_tools: Mapping[str, bool]
    director_reasoning_on: bool
    writer_reasoning_on: bool
    editor_reasoning_on: bool
    # Macro-resolved reasoning prefill per pass (text mode only; ignored when
    # that pass's reasoning is off — see reasoning_cfg).
    director_reasoning_prefill: str
    writer_reasoning_prefill: str
    editor_reasoning_prefill: str
    audit_enabled: bool
    length_guard: LengthGuard | None
    do_edit: bool
    # Local prose rewriter (Editor pass, pre-audit). Non-None means enabled;
    # deliberately independent of ``agent_on`` — it is a local model on its own
    # Local ML toggle, not one of the remote Agent passes.
    prose_rewrite: ProseRewrite | None
    # The two call surfaces for the turn. ``writer_lane`` runs the writer pass;
    # ``agent_lane`` runs director + editor. In single-model mode they are the
    # same object by construction (see :class:`ModelLane`).
    writer_lane: ModelLane
    agent_lane: ModelLane


# Fields the terminal ``_result`` event carries — a fixed subset of ``TurnState``
# so the wire shape stays stable and working fields (``writer_content``,
# ``writer_lorebook_block``, etc.) stay off the wire. Every name here is a
# ``TurnState`` field with a default, so the dict rehydrates cleanly via
# ``TurnState(**event["data"])``.
_RESULT_FIELDS = (
    "active_moods",
    "agent_raw",
    "calls",
    "latency",
    "effective_msg",
    "resp_text",
    "writer_draft",
    "inj_block",
    "extra_fields",
    "progressive_fields",
    "reasoning_director",
    "reasoning_writer",
    "reasoning_editor",
    "feedback_values",
    "direction_notes",
    "staged_attachments",
    "staged_message_state",
    "macro_choices",
    "world_proposals",
)


# Fields a group exchange's shared Director run hands to every speaker's turn.
# The Director, its per-fragment steps and the pre-writer note step all run once
# for the whole exchange (``entrypoints._generate_group_exchange``), so each
# speaker's ``TurnState`` starts from that one result rather than re-deriving it.
# Named here for the reason ``_RESULT_FIELDS`` is: the orchestrator would
# otherwise keep a hand-copied list of this class's field names, and a director
# output added below would silently stop reaching speakers 2..n.
#
# Everything the Director writes, plus the two seeds it reads (``active_moods``,
# ``macro_choices``) and the notes the exchange's pre-writer step recorded.
_DIRECTOR_SEED_FIELDS = (
    "active_moods",
    "macro_choices",
    "agent_raw",
    "calls",
    "latency",
    "extra_fields",
    "progressive_fields",
    "selected_lorebook_entries",
    "inj_block",
    "scene_direction",
    "writer_lorebook_block",
    "reasoning_director",
    "direction_notes",
)


# Fields seeding ``PostCtx.director_output`` — the read-only director view a
# post-pipeline workflow hook sees. A named subset of ``TurnState`` (same pattern
# as ``_RESULT_FIELDS``) so a field rename is caught here rather than silently
# drifting the orchestrator's hand-built dict.
_DIRECTOR_OUTPUT_FIELDS = (
    "active_moods",
    "agent_raw",
    "calls",
    "latency",
    "extra_fields",
    "progressive_fields",
)


@dataclass(slots=True)
class TurnState:
    """Mutable state threaded through all three pass stages, then consumed by persistence.

    Seeded at the start of ``_run_pipeline`` from the director state and user
    message; mutated by each stage; serialized into the ``_result`` event by
    ``as_result_event_data``; then rehydrated from that dict by persistence.
    Every field has a default so a partially-completed turn (aborted or under
    test) still produces a valid instance.

    Progressive seed/output handling lives in the director pass (see
    ``passes/director/progressive.py``); ``progressive_fields`` here is the
    persisted output, parallel to ``active_moods``. ``staged_attachments`` /
    ``staged_message_state`` are set by the orchestrator from post-pipeline
    workflow hooks just before ``_result`` is emitted.
    """

    # --- seeds / inputs ---
    user_message: str = ""
    effective_msg: str = ""
    active_moods: list[str] = field(default_factory=list)
    # Per-conversation {{random}} picks for fragment text: seeded from the
    # committed director state, extended by the director stage when a fragment
    # with a fresh macro renders, persisted back with the rest of the state.
    macro_choices: dict[str, str] = field(default_factory=dict)

    # --- director outputs ---
    agent_raw: str = ""
    calls: list[dict] = field(default_factory=list)
    latency: int = 0
    extra_fields: dict = field(default_factory=dict)
    progressive_fields: dict = field(default_factory=dict)
    selected_lorebook_entries: list[str] = field(default_factory=list)
    inj_block: str = ""
    # Scene Direction before the direction-notes block is appended; read by the
    # pre-writer notes step so the notes are not listed to it a second time.
    scene_direction: str = ""
    writer_lorebook_block: str = ""

    # --- writer / editor outputs ---
    resp_text: str = ""
    # Writer text after any group-speaker label has been stripped and inline
    # macros have been frozen, but before the local rewriter, Editor, or
    # post-pipeline workflows modify it. This travels in ``_result`` so
    # persistence can retain the source for an on-demand local rewrite later.
    writer_draft: str = ""
    writer_content: str | list[ContentPart] = ""
    reasoning_director: str = ""
    reasoning_writer: str = ""
    reasoning_editor: str = ""
    feedback_values: dict = field(default_factory=dict)
    direction_notes: list[dict] = field(default_factory=list)

    # --- post-pipeline workflow staging (set by the orchestrator) ---
    staged_attachments: list[dict] = field(default_factory=list)
    staged_message_state: dict = field(default_factory=dict)

    # --- Dynamic Worlds (set by the proposal stage, staged by persistence) ---
    # The validated, not-yet-persisted proposals, one per World the turn touched:
    # ``{world_id, base_revision, summary, operations, source_*}``. Empty when the
    # feature is off for this turn, or when the model proposed nothing. Persistence
    # turns each into a pending ``world_changesets`` row once the assistant message
    # has an id -- a changeset must name its source message, and no earlier point
    # knows it. One per World rather than one per turn because each World has its
    # own ``content_revision`` to race against and its own review queue.
    world_proposals: list[dict] = field(default_factory=list)

    def seed_from(self, director_seed: TurnState) -> None:
        """Adopt one exchange-wide Director result as this speaker's starting point.

        Mutable containers are **copied**, not shared: every speaker of an
        exchange seeds from the same object, and the post-turn steps that run on
        the last of them (``direction_notes.extend``, ``calls.append``) would
        otherwise reach back into a reply that has already been serialized.
        """
        for name in _DIRECTOR_SEED_FIELDS:
            value = getattr(director_seed, name)
            if isinstance(value, list):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            setattr(self, name, value)

    def as_result_event_data(self) -> dict:
        """Return the result-subset dict for the ``_result`` SSE envelope.

        Shallow copy on purpose: ``staged_attachments`` carries raw artifact bytes.
        """
        return {name: getattr(self, name) for name in _RESULT_FIELDS}

    def as_director_output(self) -> dict:
        """Return the director-output subset seeding ``PostCtx.director_output``.

        The plain dict the orchestrator hands to post-pipeline workflow hooks
        (wrapped read-only by the bridge). Co-located with ``_RESULT_FIELDS`` so
        a field rename surfaces here instead of silently drifting.
        """
        return {name: getattr(self, name) for name in _DIRECTOR_OUTPUT_FIELDS}


@dataclass(frozen=True, slots=True)
class LorebookTurn:
    """The lorebook inputs for one main-pipeline turn.

    Bundles the per-turn lorebook inputs threaded through the pipeline.
    ``block`` and ``catalog`` are the Director-facing context and are
    mutually exclusive by mode (kept separate because they inject at different
    positions in the Director prompt). ``writer_block`` derives the final block
    shown to the writer. Constant entries are not part of any trailing block —
    they ride the cached system prefix (``context._build_prefix_from_ctx``), or
    ``depth_block`` when they set ``at_depth``; ``entries`` still carries the
    full pool so the catalog/selection layer sees everything.

    The selection/rendering it delegates to lives in the pure ``lorebook`` layer
    (``backend/inference/lorebook.py``); this bundle is the pipeline-turn view
    that threads those inputs from ``_prepare_turn`` to ``director_stage``.
    """

    entries: Sequence[Mapping[str, Any]]
    messages: Sequence[Mapping[str, Any]]
    agentic: bool
    block: str = ""  # Director-facing lore context (substring mode; "" when agentic)
    catalog: str = ""  # Director-facing pick catalog (agentic mode; "" otherwise)
    # Rendered once per turn by ``_prepare_turn``: the ``constant`` + ``at_depth``
    # entries, macros resolved unseeded. Frozen here so the writer and the editor
    # replaying its content see the same {{roll}} values.
    depth_block: str = ""

    @property
    def scan_depth(self) -> int:
        return AGENTIC_LOREBOOK_SCAN_DEPTH if self.agentic else LOREBOOK_SCAN_DEPTH

    def writer_block(self, director_selected: Sequence[str], macros: Macros | None = None) -> str:
        """The trailing lorebook block injected into the writer prompt.

        In substring mode this equals the Director-facing ``block`` already
        computed up front (same entries/messages/depth), so it is reused rather
        than recomputed. In agentic mode it is the union of the current-turn
        keyword scan and the Director's *director_selected* picks. Constant
        entries never appear here — they ride the cached system prefix.
        """
        if not self.agentic:
            return self.block
        return compute_lorebook_block(
            self.entries,
            self.messages,
            scan_depth=self.scan_depth,
            director_selected=director_selected,
            macros=macros,
        )


@dataclass(frozen=True, slots=True)
class WorldProposalTurn:
    """Identity of the Worlds a completed turn may propose changes to.

    Deliberately carries no entries and no revisions: the proposal step re-reads
    both immediately before it runs, so a proposal is always based on each World
    as it stands *after* the turn's own latency rather than as it stood when the
    turn began.

    The targets are every World that is *enabled* -- feeding this turn's prompt,
    so its lore is what the exchange was played against -- and has its own
    ``dynamic_enabled`` opt-in (``predicates.world_proposal_active``). A World the
    user has not opted in is never a target, and a disabled one is not either: it
    contributed nothing to the scene, so nothing in the scene is evidence about
    it. ``user_message`` is the *semantic* user message: on the steered paths it
    is the original message, not Orb's OOC steering prompt. The two labels are
    denormalised into each changeset so applied history stays readable after the
    chat is deleted.
    """

    world_ids: tuple[str, ...]
    conversation_id: str
    user_message: str
    character_label: str = ""
    conversation_label: str = ""


@dataclass(frozen=True, slots=True)
class SheetUpdateTurn:
    """What a finished group exchange may propose sheet updates about.

    Carries the exchange's *evidence* rather than its identity, because unlike a
    World the target has no revision to re-read against: a sheet update is
    derived from the prose of one exchange, and that prose is only assembled once,
    in the exchange driver. ``lines`` are the exchange's replies as
    ``(speaker_name, text)`` in order, already persisted; the running speaker's
    own draft is appended by the stage from ``TurnState.resp_text``, because it
    is not persisted until after the stage runs.

    ``member_ids`` are the members the exchange actually *touched* — the ones that
    spoke. Cast-wide would be one billed call per member per exchange to tell a
    silent member nothing happened to them. A member that spoke twice in one
    exchange still gets one call, so the stage de-duplicates rather than trusting
    this to be a set.

    ``speaker_name`` labels the running speaker's unpersisted draft in the
    transcript. Named rather than inferred from the last id, so the stage does
    not depend on an ordering convention it cannot check.
    """

    conversation_id: str
    exchange_id: str
    member_ids: tuple[str, ...]
    user_message: str
    speaker_name: str = ""
    lines: tuple[tuple[str, str], ...] = ()
