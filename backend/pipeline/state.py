"""
state.py — The per-turn contract dataclasses shared across passes.

These three dataclasses are the turn-state contract every pass reads: the
orchestrator builds them and the director / writer / editor passes consume them.
They live here — a focused leaf the passes point *down* into — rather than at the
top in ``orchestrator.py``, so the dependency runs one direction (passes →
pipeline_state) instead of the passes reaching up into the coordinator.

Only the dataclass *shapes* live here. Their construction and behaviour
(``_resolve_pipeline_config``, ``_make_result``, ``is_dual_model``, …) stay in
``orchestrator.py``; ``_PipelineResult`` likewise stays there, being
pipeline-internal and not consumed by any pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from ..core import ContentPart
from ..inference import CachedBase, LLMClient
from .passes.editor.length_guard import LengthGuard


@dataclass(frozen=True)
class ModelLane:
    """One model's call surface for the turn: a client paired with its
    byte-identical cached bottom (prefix + tools + model + the macro ``resolve``
    hook that scrubs placeholders from the final wire bytes).

    A turn has two lanes — ``writer`` and ``agent`` (director + editor). In
    single-model mode they are the *same object* (the writer's lane is reused for
    the agent), so the byte-identity invariant "director + editor + writer ride
    the same base" is structural, not a convention each call site must honour. In
    dual-model mode they are distinct: the agent lane carries the agent server's
    client, its own prefix + tool blob, and the agent model; the writer lane
    carries the writer client with an empty tools blob (Invariant 5).

    ``reasoning`` stays per-pass (director and editor share the agent lane but
    toggle reasoning independently), so it is not part of the lane.
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
    # The two call surfaces for the turn. ``writer_lane`` runs the writer pass;
    # ``agent_lane`` runs director + editor. In single-model mode they are the
    # same object by construction (see :class:`ModelLane`).
    writer_lane: ModelLane
    agent_lane: ModelLane


@dataclass
class TurnState:
    """Mutable per-turn state threaded by reference through the three pass
    stages (``director_stage`` / ``writer_stage`` / ``editor_stage``).

    These were ``_run_pipeline``'s ~20 turn-state locals. The result-bound
    fields mirror ``_PipelineResult`` (and ``DirectorResult``) names so
    one name follows each value from the director pass through to persistence;
    ``_make_result`` reads this straight into a ``_PipelineResult``.

    Seeded in ``_run_pipeline`` from ``director`` (``active_moods`` and the
    progressive seed filtered to valid fragment ids) and the resolved
    ``user_message`` (``effective_msg``). ``progressive_state`` /
    ``valid_progressive_ids`` are turn inputs (not result fields): the director
    seed map and the id set used to filter director output into
    ``progressive_fields``.
    """

    # --- seeds / inputs ---
    user_message: str = ""
    effective_msg: str = ""
    active_moods: list[str] = field(default_factory=list)
    progressive_state: dict = field(default_factory=dict)
    valid_progressive_ids: set[str] = field(default_factory=set)

    # --- director outputs ---
    agent_raw: str = ""
    calls: list[dict] = field(default_factory=list)
    latency: int = 0
    rewritten_msg: str | None = None
    extra_fields: dict = field(default_factory=dict)
    progressive_fields: dict = field(default_factory=dict)
    selected_lorebook_entries: list[str] = field(default_factory=list)
    inj_block: str = ""
    writer_lorebook_block: str = ""

    # --- writer / editor outputs ---
    resp_text: str = ""
    writer_content: "str | list[ContentPart]" = ""
    reasoning_director: str = ""
    reasoning_writer: str = ""
    reasoning_editor: str = ""
    feedback_values: dict = field(default_factory=dict)
