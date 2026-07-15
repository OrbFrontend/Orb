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

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..core import ContentPart, Macros
from ..features.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    LOREBOOK_SCAN_DEPTH,
    compute_lorebook_block,
)
from ..inference import CachedBase, LLMClient
from .passes.editor.length_guard import LengthGuard


@dataclass(frozen=True)
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


@dataclass
class _PipelineConfig:
    """Resolved per-turn flags, lanes, and prefixes for ``_run_pipeline``."""

    agent_on: bool
    enabled_tools: Mapping[str, bool]
    director_reasoning_on: bool
    writer_reasoning_on: bool
    editor_reasoning_on: bool
    audit_enabled: bool
    length_guard: LengthGuard | None
    do_edit: bool
    writer_enabled_tools: Mapping[str, bool]
    # True when the writer endpoint is in text-completion mode: suppress the
    # no-tools nudge (meaningless without a rendered tool harness). The shared
    # tool blob is untouched — director/editor keep their schemas.
    writer_text_mode: bool
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


@dataclass
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
    writer_content: "str | list[ContentPart]" = ""
    reasoning_director: str = ""
    reasoning_writer: str = ""
    reasoning_editor: str = ""
    feedback_values: dict = field(default_factory=dict)
    direction_notes: list[dict] = field(default_factory=list)

    # --- post-pipeline workflow staging (set by the orchestrator) ---
    staged_attachments: list[dict] = field(default_factory=list)
    staged_message_state: dict = field(default_factory=dict)

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


@dataclass(frozen=True)
class LorebookTurn:
    """The lorebook inputs for one main-pipeline turn.

    Bundles the per-turn lorebook inputs threaded through the pipeline.
    ``block`` and ``catalog`` are the Director-facing context and are
    mutually exclusive by mode (kept separate because they inject at different
    positions in the Director prompt). ``writer_block`` derives the final block
    shown to the writer.

    The selection/rendering it delegates to lives in the pure ``lorebook`` layer
    (``backend/features/lorebook/activation.py``); this bundle is the pipeline-turn view
    that threads those inputs from ``_prepare_turn`` to ``director_stage``.
    """

    entries: Sequence[Mapping[str, Any]]
    messages: Sequence[Mapping[str, Any]]
    agentic: bool
    block: str = ""  # Director-facing lore context (substring mode; "" when agentic)
    catalog: str = ""  # Director-facing pick catalog (agentic mode; "" otherwise)

    @property
    def scan_depth(self) -> int:
        return AGENTIC_LOREBOOK_SCAN_DEPTH if self.agentic else LOREBOOK_SCAN_DEPTH

    def writer_block(self, director_selected: Sequence[str], macros: Macros | None = None) -> str:
        """The lorebook block injected into the writer prompt.

        In substring mode this equals the Director-facing ``block`` already
        computed up front (same entries/messages/depth), so it is reused rather
        than recomputed. In agentic mode it is the union of constants, the
        current-turn keyword scan, and the Director's *director_selected* picks.
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
