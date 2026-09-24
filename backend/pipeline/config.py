"""Resolve per-turn settings, model lanes, and tool schemas."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..core import STATE_FIELD_TYPE, ChatMessage, Macros
from ..database.models import PhraseGroup
from ..inference import (
    CachedBase,
    LLMClient,
)
from ..prompting.tool_catalog import enabled_schemas
from ..prompting.tool_schemas import build_state_tool
from ..workflows.enablement import disabled_workflow_tool_names
from .passes.director import build_direct_scene_override
from .passes.editor import (
    _feedback_active,
    build_feedback_override,
    post_processing_active,
)
from .passes.editor.length_guard import (
    LengthGuard,
    apply_length_guard_tools,
    resolve_length_guard,
)
from .passes.state import StateContract
from .passes.state.contract import NON_SCENE_FIELD_TYPES
from .predicates import agent_enabled, is_dual_model
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
        writer_lane=writer_lane,
        agent_lane=agent_lane,
    )


def _split_interactive_fragments(
    fragments: Sequence[Mapping[str, Any]],
) -> tuple[
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
    list[Mapping[str, Any]],
]:
    """Split fragments by pipeline stage; decisions run before the Director.

    Returns ``(scene, feedback, state, post_processing)``. Scene fragments are
    the Director's per-turn values. State fragments are routed by their own
    settings (see :class:`StateContract`).
    """
    scene = [df for df in fragments if df.get("field_type") not in NON_SCENE_FIELD_TYPES]
    feedback = [df for df in fragments if df.get("field_type") == "feedback"]
    state = [df for df in fragments if df.get("field_type") == STATE_FIELD_TYPE]
    post_processing = [df for df in fragments if df.get("field_type") == "post_processing"]
    return scene, feedback, state, post_processing


def _build_writer_tools_blob(
    settings: Mapping[str, Any],
    interactive_fragments: Sequence[Mapping[str, Any]],
    enabled_tools: Mapping[str, bool],
    *,
    agentic_lorebook: bool = False,
    dynamic_world: bool = False,
    grouped: bool = False,
    state_contract: StateContract | None = None,
) -> tuple[dict, dict[str, bool]]:
    """Build the tool schemas shared by cached calls.

    Returns ``(schema_overrides, enabled_tools)``: the overrides, and a copy of
    *enabled_tools* with every tool this turn's features and fragments switch on.

    *state_contract* is the turn's captured state configuration; omitted, it is
    captured from the same *settings* and fragments.
    """
    enabled_tools = dict(enabled_tools)
    _, feedback_fragments, state_fragments, post_processing_fragments = _split_interactive_fragments(interactive_fragments)
    contract = state_contract or StateContract.capture(settings, state_fragments)
    direct_scene = build_direct_scene_override(contract.direct_scene_rows(interactive_fragments), grouped=grouped)
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
    if post_processing_active(post_processing_fragments, agent_on=agent_enabled(settings)):
        enabled_tools["editor_search_replace"] = True
    # The union of every fragment the state tool may carry this turn, before or
    # after the Writer, so both steps share one byte-stable blob. The schema
    # depends only on configuration; state writes never rebuild it.
    if tool_fragments := contract.tool_fragments():
        overrides["update_state"] = build_state_tool(tool_fragments)
        enabled_tools["update_state"] = True
    return overrides, enabled_tools
