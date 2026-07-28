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

from ..core import ContentPart, Macros, WireMessage
from ..features.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    LOREBOOK_SCAN_DEPTH,
    compute_lorebook_block,
)
from ..inference import CachedBase, LLMClient
from ..workflows import WriterToolBinding
from .passes.editor.length_guard import LengthGuard
from .replay import CANONICAL_DRAFT_BLOCK, WriterReplay


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


@dataclass(frozen=True, slots=True)
class ExtensionContext:
    """This turn's community-extension context, rendered per prompt target.

    Two pre-rendered strings rather than a list of blocks, because the passes
    that consume them must not be able to reorder, filter, or re-attribute what
    an extension contributed -- they concatenate a string into a trailing
    message and nothing else.

    Trailing, never prefixed. Extension context is dynamic by nature, and a
    dynamic block in the cached system prefix would invalidate the whole
    conversation's KV cache every turn it changed. These land beside the
    lorebook and Scene Direction blocks, where the bytes were going to differ
    each turn anyway.
    """

    director: str = ""
    writer: str = ""

    @classmethod
    def from_blocks(cls, blocks: Sequence[Mapping[str, Any]]) -> ExtensionContext:
        """Render collected blocks into one string per target.

        Sorted by extension id, then label: two users with the same set of
        packages get byte-identical prompts regardless of the order they
        installed them in.
        """
        ordered = sorted(blocks, key=lambda b: (b.get("extension_id", ""), b.get("label", "")))

        def render(target: str) -> str:
            sections = [f"**{b['label']}**\n{b['text']}" for b in ordered if target in b.get("targets", ())]
            return "\n\n".join(sections)

        return cls(director=render("director"), writer=render("writer"))


@dataclass(frozen=True, slots=True)
class WriterToolPolicy:
    """What this turn lets the Writer call, and what it is told about it.

    Separate from the binding because the two answer different questions and
    have different authorities. The *policy* is prompt text: guidance that
    improves tool choice, especially in single-model mode where the shared base
    also carries the agent's schemas. The *binding* is the captured allowlist,
    and it is what the host validates every returned call against. A model that
    ignores the prompt still cannot reach anything the binding does not name.

    ``binding`` is ``None`` on every turn with no eligible selected resolver,
    which is every turn on an install with no v2 package -- and that case has to
    produce byte-identical request shapes to the ones Orb sent before Writer
    tools existed.
    """

    binding: WriterToolBinding | None = None
    description: str = ""
    parameter_summary: str = ""
    diagnostic: str = ""
    """Why an otherwise-selected resolver is not active this turn.

    Endpoint incompatibility, mostly. Kept as host diagnostic state rather than
    failing the turn: a Writer tool that cannot run safely on the configured
    endpoint means the turn takes the ordinary no-tools path, not that the user
    loses their reply."""

    @property
    def wire_name(self) -> str:
        return self.binding.wire_name if self.binding is not None else ""

    @property
    def label(self) -> str:
        return self.binding.label if self.binding is not None else ""

    @property
    def active(self) -> bool:
        return self.binding is not None


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
    # The two lanes' schema sets, kept apart because they answer two questions
    # that used to be conflated. ``agent_tool_schemas`` is "which agent tools
    # belong in the blob"; ``writer_tool_schemas`` is "what the Writer may
    # invoke". In dual-model mode the second used to be empty *because every
    # existing tool was an agent tool*, which was an optimization for the
    # registry of the day rather than a permanent invariant.
    agent_tool_schemas: tuple[dict, ...]
    writer_tool_schemas: tuple[dict, ...]
    writer_tool_policy: WriterToolPolicy
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
    "director_fields",
    "extra_fields",
    "progressive_fields",
    "fragment_diagnostics",
    "reasoning_director",
    "reasoning_writer",
    "reasoning_editor",
    "feedback_values",
    "direction_notes",
    "staged_attachments",
    "staged_message_state",
    "macro_choices",
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
    "director_fields",
    "extra_fields",
    "progressive_fields",
    "fragment_diagnostics",
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
    # Raw values returned by ``direct_scene``. They are validated/reduced before
    # anything reaches ``extra_fields`` or persisted progressive state.
    director_fields: dict = field(default_factory=dict)
    extra_fields: dict = field(default_factory=dict)
    progressive_fields: dict = field(default_factory=dict)
    fragment_diagnostics: list[dict[str, str]] = field(default_factory=list)
    selected_lorebook_entries: list[str] = field(default_factory=list)
    inj_block: str = ""
    # Scene Direction before the direction-notes block is appended; read by the
    # pre-writer notes step so the notes are not listed to it a second time.
    scene_direction: str = ""
    writer_lorebook_block: str = ""

    # --- writer / editor outputs ---
    resp_text: str = ""
    writer_content: str | list[ContentPart] = ""
    # The Writer's own message transcript for this turn, kept only long enough
    # to replay downstream. Ephemeral by design: it holds a tool call and a tool
    # result, and persisting those would make hidden protocol messages part of
    # the conversation the next turn loads. ``_RESULT_FIELDS`` deliberately
    # omits it, so it never reaches the ``_result`` event or persistence.
    writer_trace: list[WireMessage] = field(default_factory=list)
    writer_tool_used: bool = False
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

    def writer_replay(self, draft: str, *, dual_model: bool) -> WriterReplay:
        """The messages a downstream agent-lane call should replay.

        Returns the pre-Writer-tool shape whenever it is the right one, which is
        every turn without a tool call and every dual-model turn. That is not a
        fallback: in dual-model mode the agent base does not declare the Writer's
        tool, so a historical call to it would be a message the agent model was
        never told about -- and there is no Writer-lane cache on that server to
        extend by replaying it.
        """
        if dual_model or not self.writer_tool_used or not self.writer_trace:
            normalized: tuple[WireMessage, ...] = (
                {"role": "user", "content": self.writer_content},
                {"role": "assistant", "content": draft},
            )
            return WriterReplay(messages=normalized)
        return WriterReplay(
            messages=tuple(self.writer_trace),
            canonical_draft_block=CANONICAL_DRAFT_BLOCK.format(draft=draft),
        )

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
    they ride the cached system prefix (``context._build_prefix_from_ctx``);
    ``entries`` still carries the full pool so the catalog/selection layer sees
    everything.

    The selection/rendering it delegates to lives in the pure ``lorebook`` layer
    (``backend/inference/lorebook.py``); this bundle is the pipeline-turn view
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
