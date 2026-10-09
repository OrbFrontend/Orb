"""Resolve per-turn settings, model lanes, and tool schemas."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from ..core import STATE_FIELD_TYPE, ChatMessage, Macros
from ..core.settings import Settings
from ..database.models import PhraseGroup
from ..inference import CachedBase, LLMClient, lane_template_thinking
from ..prompting.tool_catalog import enabled_schemas
from ..prompting.tool_schemas import build_state_tool
from ..workflows.enablement import disabled_workflow_tool_names
from .passes.director import build_direct_scene_override
from .passes.editor import build_feedback_override, feedback_active
from .passes.editor.length_guard import LengthGuard, resolve_length_guard
from .passes.state import StateContract
from .passes.state.contract import NON_SCENE_FIELD_TYPES
from .predicates import agent_enabled, is_dual_model
from .state import ModelLane, PipelineConfig
from .tools import AGENT_PASS_TOOLS


def resolve_pipeline_config(
    settings: Settings,
    enabled_tools: Mapping[str, bool],
    *,
    macros: Macros,
    client: LLMClient,
    agent_client: LLMClient | None,
    agent_prefix: list[ChatMessage] | None,
    prefix: list[ChatMessage],
    phrase_bank: list[PhraseGroup] | None,
    schema_overrides: Mapping[str, dict],
) -> PipelineConfig:
    """Build the immutable per-turn config.

    Resolves feature flags (audit, length guard, per-pass reasoning), builds the writer and agent lanes, and returns a
    :class:`PipelineConfig`. Called once per turn by ``run_pipeline``.
    """
    # Drop a disabled workflow's tools from the per-turn blob at the single chokepoint that builds it, covering both the
    # standing enabled_tools map and any per-turn enable. Empty no-op when no disabled workflow owns tools.
    enabled_tools = {k: v for k, v in enabled_tools.items() if k not in disabled_workflow_tool_names(settings)}

    agent_on = agent_enabled(settings)
    reasoning_passes = settings.get("reasoning_enabled_passes") or {}
    prefills = settings.get("reasoning_prefill_passes") or {}

    def _prefill(key: str) -> str:
        # resolve_message is seeded by conversation id, so {{random}}/{{roll}} pin per conversation exactly like fragment text --
        # the tail stays byte-stable turn over turn.
        raw = str(prefills.get(key) or "")
        return macros.resolve_message(raw) if raw else ""

    audit_enabled = agent_on and bool(enabled_tools.get("editor_apply_patch", False)) and phrase_bank is not None
    length_guard: LengthGuard | None = resolve_length_guard(settings, agent_on)

    # The toggles gate the passes; the blob offers the Agent's own tools whenever the Agent is on, so switching the Director,
    # the auditor or the length guard never rewrites the cached tools region.
    active_tools = enabled_tools
    if agent_on:
        enabled_tools = {**enabled_tools, **dict.fromkeys(AGENT_PASS_TOOLS, True)}

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
            template_thinking=lane_template_thinking(reasoning_passes, lane="writer", separate_agent_lane=dual_model),
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
                template_thinking=lane_template_thinking(reasoning_passes, lane="agent", separate_agent_lane=True),
            ),
        )
    else:
        # Single-model: agent shares the writer's lane (same KV cache base).
        agent_lane = writer_lane

    return PipelineConfig(
        agent_on=agent_on,
        enabled_tools=enabled_tools,
        active_tools=active_tools,
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


def split_interactive_fragments(
    fragments: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    """Split fragments by pipeline stage; decisions run before the Director.

    Returns ``(scene, feedback, state, post_processing)``. Scene fragments are the Director's per-turn values. State fragments
    are routed by their own settings (see :class:`StateContract`).
    """
    scene = [df for df in fragments if df.get("field_type") not in NON_SCENE_FIELD_TYPES]
    feedback = [df for df in fragments if df.get("field_type") == "feedback"]
    state = [df for df in fragments if df.get("field_type") == STATE_FIELD_TYPE]
    post_processing = [df for df in fragments if df.get("field_type") == "post_processing"]
    return scene, feedback, state, post_processing


def _without_required(schema: dict) -> dict:
    """Drop top-level ``required`` from a fragment-built schema on the shared blob; calls state their live fields instead."""
    schema["function"]["parameters"]["required"] = []
    return schema


def _names_only(schema: dict, fragment_ids: Collection[str]) -> dict:
    """Drop the description from each fragment-built property on the shared blob.

    Every defined fragment rides the blob, enabled or not, so its description would reach every call on the lane -- the Writer's
    included in single-model mode. Each fragment property keeps its name and type; the pass that fills a live field states its
    description in the trailing request and the per-call ``json_schema``. Fixed, code-authored properties keep theirs.
    """
    for key, prop in schema["function"]["parameters"]["properties"].items():
        if key in fragment_ids:
            prop.pop("description", None)
    return schema


def build_writer_tools_blob(
    settings: Settings,
    defined_fragments: Sequence[Mapping[str, Any]],
    enabled_tools: Mapping[str, bool],
    *,
    agentic_lorebook: bool = False,
    dynamic_world: bool = False,
    grouped: bool = False,
) -> tuple[dict, dict[str, bool]]:
    """Build shared schemas and return (overrides, enabled_tools copy).

    Include all defined fragments so enablement never changes the cached blob. Properties expose names/types only; trailing
    requests supply enabled fragment text and narrow the live call.
    """
    enabled_tools = dict(enabled_tools)
    agent_on = agent_enabled(settings)
    _, feedback_fragments, state_fragments, _ = split_interactive_fragments(defined_fragments)
    contract = StateContract.defined(settings, state_fragments)
    scene_rows = contract.direct_scene_rows(defined_fragments)
    direct_scene = build_direct_scene_override(scene_rows, grouped=grouped)
    # Required stays on the wire, where schema-decoding upstreams honor it, except for one-field steps (docs/architecture/kv-cache.md).
    if settings.get("director_individual_fragments", 0):
        direct_scene = _without_required(direct_scene)
    overrides: dict = {"direct_scene": _names_only(direct_scene, {row["id"] for row in scene_rows})}
    if agentic_lorebook:
        enabled_tools["select_lorebook"] = True
    if dynamic_world:
        enabled_tools["propose_world_changes"] = True
    if feedback_active(feedback_fragments, agent_on=agent_on):
        overrides["give_feedback"] = _names_only(
            _without_required(build_feedback_override(feedback_fragments)), {row["id"] for row in feedback_fragments}
        )
        enabled_tools["give_feedback"] = True
    # The union of every fragment the state tool may carry, before or after the Writer, so both steps share one byte-stable
    # blob. The schema depends only on configuration; state writes never rebuild it.
    if tool_fragments := contract.tool_fragments():
        overrides["update_state"] = _names_only(build_state_tool(tool_fragments), {fragment.id for fragment in tool_fragments})
        enabled_tools["update_state"] = True
    return overrides, enabled_tools
