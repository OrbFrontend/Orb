"""Resolve per-turn settings, model lanes, and tool schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..core import ChatMessage, Macros
from ..database.models import PhraseGroup
from ..features.prose_rewriter import ProseRewriteConfig
from ..features.prose_rewriter import resolve_config as resolve_prose_rewrite
from ..inference import (
    CachedBase,
    LLMClient,
)
from ..prompting.tool_catalog import enabled_schemas
from ..prompting.tool_schemas import build_direction_note_tool
from ..workflows.enablement import disabled_workflow_tool_names
from .passes.director import build_direct_scene_override
from .passes.editor import _feedback_active, build_feedback_override
from .passes.editor.length_guard import (
    LengthGuard,
    apply_length_guard_tools,
    resolve_length_guard,
)
from .predicates import agent_enabled, direction_note_recording_active, is_dual_model
from .state import ModelLane, _PipelineConfig


def _resolve_pipeline_config(
    settings: Mapping[str, Any],
    enabled_tools: Mapping[str, bool],
    *,
    macros: Macros,
    client: LLMClient,
    agent_client: LLMClient | None,
    agent_prefix: list[ChatMessage] | None,
    prefix: list[ChatMessage],
    phrase_bank: list[PhraseGroup] | None,
    schema_overrides: Mapping[str, dict],
) -> _PipelineConfig:
    """Build the immutable per-turn config.

    Resolves feature flags (audit, length guard, per-pass reasoning), builds the
    writer and agent lanes, and returns a :class:`_PipelineConfig`. Called once
    per turn by ``_run_pipeline``.
    """
    # Drop a disabled workflow's tools from the per-turn blob at the single
    # chokepoint that builds it, covering both the standing enabled_tools map and
    # any per-turn enable. Empty no-op when no disabled workflow owns tools.
    enabled_tools = {k: v for k, v in enabled_tools.items() if k not in disabled_workflow_tool_names(settings)}

    agent_on = agent_enabled(settings)
    reasoning_passes = settings.get("reasoning_enabled_passes") or {}
    prefills = settings.get("reasoning_prefill_passes") or {}

    def _prefill(key: str) -> str:
        # resolve_message is seeded by conversation id, so {{random}}/{{roll}} pin
        # per conversation exactly like fragment text — the tail stays byte-stable
        # turn over turn.
        raw = str(prefills.get(key) or "")
        return macros.resolve_message(raw) if raw else ""

    audit_enabled = agent_on and bool(enabled_tools.get("editor_apply_patch", False)) and phrase_bank is not None

    # editor_rewrite is mirrored into the schema blob when the length guard is on.
    length_guard: LengthGuard | None = resolve_length_guard(settings, agent_on)
    enabled_tools = apply_length_guard_tools(enabled_tools, length_guard)

    # No `agent_on` conjunction, unlike every other editor feature above: the
    # prose rewriter is a local model gated only by its own Local ML toggle,
    # its selected variant being on disk, and a llama-server binary resolving.
    # It contributes no tool schema, so the cached prefix is untouched.
    prose_rewrite: ProseRewriteConfig | None = resolve_prose_rewrite(settings)

    # In dual-model mode the writer's KV cache is disjoint; skip tool schemas there.
    dual_model = is_dual_model(agent_client)
    writer_enabled_tools = {} if dual_model else enabled_tools

    writer_lane = ModelLane(
        client=client,
        base=CachedBase(
            prefix=tuple(prefix),
            tools=tuple(enabled_schemas(writer_enabled_tools, schema_overrides)),
            model=settings["model_name"],
            resolve=macros.resolve_prompt_messages,
        ),
    )
    if dual_model:
        assert agent_client is not None
        agent_lane = ModelLane(
            client=agent_client,
            base=CachedBase(
                prefix=tuple(agent_prefix or prefix),
                tools=tuple(enabled_schemas(enabled_tools, schema_overrides)),
                model=settings.get("agent_model_name", settings["model_name"]),
                resolve=macros.resolve_prompt_messages,
            ),
        )
    else:
        # Single-model: agent shares the writer's lane (same KV cache base).
        agent_lane = writer_lane

    return _PipelineConfig(
        agent_on=agent_on,
        enabled_tools=enabled_tools,
        director_reasoning_on=bool(reasoning_passes.get("director", False)),
        writer_reasoning_on=bool(reasoning_passes.get("writer", False)),
        editor_reasoning_on=bool(reasoning_passes.get("editor", False)),
        director_reasoning_prefill=_prefill("director"),
        writer_reasoning_prefill=_prefill("writer"),
        editor_reasoning_prefill=_prefill("editor"),
        audit_enabled=audit_enabled,
        length_guard=length_guard,
        do_edit=audit_enabled or length_guard is not None,
        prose_rewrite=prose_rewrite,
        writer_lane=writer_lane,
        agent_lane=agent_lane,
    )


def _split_interactive_fragments(
    fragments: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split interactive fragments into writer, feedback, and direction-note groups.

    Feedback-type fragments surface to the user via the post-writer feedback step;
    direction-note-type fragments feed the direction-note step; all others shape the
    ``direct_scene`` tool and Scene Direction block. The three groups are disjoint.
    """
    writer = [df for df in fragments if df.get("field_type") not in ("feedback", "direction_note")]
    feedback = [df for df in fragments if df.get("field_type") == "feedback"]
    direction_note_fragments = [df for df in fragments if df.get("field_type") == "direction_note"]
    return writer, feedback, direction_note_fragments


def _build_writer_tools_blob(
    settings: Mapping[str, Any],
    interactive_fragments: Sequence[Mapping[str, Any]],
    enabled_tools: dict,
    *,
    agentic_lorebook: bool = False,
    dynamic_world: bool = False,
    grouped: bool = False,
) -> dict:
    """Build the tool schemas shared by cached calls."""
    writer_fragments, feedback_fragments, direction_note_fragments = _split_interactive_fragments(interactive_fragments)
    direct_scene = build_direct_scene_override(writer_fragments, grouped=grouped)
    # Per-fragment mode fills one field per call, so requiredness on the shared blob
    # is meaningless -- and a non-empty `required` contradicts the "Fill ONLY X, leave
    # others empty" step prompt, which confuses the reasoning pass on endpoints that
    # can't grammar-narrow the call (no structured-tool-calls profile). Drop it.
    if bool(settings.get("director_individual_fragments", 0)):
        direct_scene["function"]["parameters"]["required"] = []
    overrides: dict = {"direct_scene": direct_scene}
    if agentic_lorebook:
        enabled_tools["select_lorebook"] = True
    if dynamic_world:
        enabled_tools["propose_world_changes"] = True
    if _feedback_active(settings, feedback_fragments, agent_on=agent_enabled(settings)):
        overrides["give_feedback"] = build_feedback_override(feedback_fragments)
        enabled_tools["give_feedback"] = True
    if direction_note_recording_active(settings, direction_note_fragments, agent_on=agent_enabled(settings)):
        overrides["record_direction_note"] = build_direction_note_tool(direction_note_fragments)
        enabled_tools["record_direction_note"] = True
    return overrides
