"""Per-turn dataclasses shared across pipeline passes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..core import ChatMessage, ContentPart, Macros, joined_delta
from ..features.prose_rewriter import ProseRewriteConfig
from ..inference import CachedBase, LLMClient
from ..prompting.lorebook import (
    AGENTIC_LOREBOOK_SCAN_DEPTH,
    LOREBOOK_SCAN_DEPTH,
    compute_lorebook_block,
)
from .passes.editor.length_guard import LengthGuard


@dataclass(frozen=True, slots=True)
class ModelLane:
    """An LLM client paired with its frozen prompt base."""

    client: LLMClient
    base: CachedBase

    def sends_tool_schemas(self, trailing: Sequence[ChatMessage], *, tools_in_prompt: bool = True) -> bool:
        """Return whether a call extending this lane sends tool schemas."""
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
    prose_rewrite: ProseRewriteConfig | None
    # The two call surfaces for the turn. ``writer_lane`` runs the writer pass;
    # ``agent_lane`` runs director + editor. In single-model mode they are the
    # same object by construction (see :class:`ModelLane`).
    writer_lane: ModelLane
    agent_lane: ModelLane


# Fields included in the terminal ``_result`` event.
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


# Fields copied from the shared Director result to each group speaker.
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


# Fields exposed as the read-only Director output to post-pipeline workflows.
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
    """Mutable state shared by the turn stages and persistence."""

    user_message: str = ""
    effective_msg: str = ""
    active_moods: list[str] = field(default_factory=list)
    # Per-conversation macro choices, persisted with Director state.
    macro_choices: dict[str, str] = field(default_factory=dict)

    agent_raw: str = ""
    calls: list[dict] = field(default_factory=list)
    latency: int = 0
    extra_fields: dict = field(default_factory=dict)
    progressive_fields: dict = field(default_factory=dict)
    selected_lorebook_entries: list[str] = field(default_factory=list)
    inj_block: str = ""
    # Scene Direction before direction notes are appended.
    scene_direction: str = ""
    writer_lorebook_block: str = ""

    resp_text: str = ""
    # Writer text before local rewriting, editing, or post-pipeline workflows.
    writer_draft: str = ""
    writer_content: str | list[ContentPart] = ""
    reasoning_director: str = ""
    reasoning_writer: str = ""
    reasoning_editor: str = ""
    feedback_values: dict = field(default_factory=dict)
    direction_notes: list[dict] = field(default_factory=list)

    staged_attachments: list[dict] = field(default_factory=list)
    staged_message_state: dict = field(default_factory=dict)

    # Validated proposals, staged after the assistant message is persisted.
    world_proposals: list[dict] = field(default_factory=list)

    def seed_from(self, director_seed: TurnState) -> None:
        """Copy the shared exchange Director result into this speaker's state."""
        for name in _DIRECTOR_SEED_FIELDS:
            value = getattr(director_seed, name)
            if isinstance(value, list):
                value = list(value)
            elif isinstance(value, dict):
                value = dict(value)
            setattr(self, name, value)

    def add_reasoning(self, pass_name: str, event: Mapping[str, Any]) -> str:
        """Append one pass delta and return the exact text to stream."""
        buffer = f"reasoning_{pass_name}"
        delta = joined_delta(getattr(self, buffer), event)
        setattr(self, buffer, getattr(self, buffer) + delta)
        return delta

    def as_result_event_data(self) -> dict:
        """Return the stable field subset for the ``_result`` SSE event."""
        return {name: getattr(self, name) for name in _RESULT_FIELDS}

    def as_director_output(self) -> dict:
        """Return the read-only Director output for post-pipeline workflows."""
        return {name: getattr(self, name) for name in _DIRECTOR_OUTPUT_FIELDS}


@dataclass(frozen=True, slots=True)
class LorebookTurn:
    """Lorebook inputs threaded through one pipeline turn."""

    entries: Sequence[Mapping[str, Any]]
    messages: Sequence[Mapping[str, Any]]
    agentic: bool
    block: str = ""  # Director-facing lore context in substring mode.
    catalog: str = ""  # Director-facing pick catalog in agentic mode.
    # Frozen so replayed prompts see the same macro values.
    depth_block: str = ""

    @property
    def scan_depth(self) -> int:
        return AGENTIC_LOREBOOK_SCAN_DEPTH if self.agentic else LOREBOOK_SCAN_DEPTH

    def writer_block(self, director_selected: Sequence[str], macros: Macros | None = None) -> str:
        """Return the lorebook block appended to the Writer prompt."""
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
    """World targets and labels for a completed turn's proposal stage."""

    world_ids: tuple[str, ...]
    conversation_id: str
    user_message: str
    character_label: str = ""
    conversation_label: str = ""


@dataclass(frozen=True, slots=True)
class SheetUpdateTurn:
    """Evidence and targets for one group's scene-sheet update stage."""

    conversation_id: str
    exchange_id: str
    member_ids: tuple[str, ...]
    user_message: str
    speaker_name: str = ""
    lines: tuple[tuple[str, str], ...] = ()
