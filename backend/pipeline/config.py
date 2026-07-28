"""
config.py — Per-turn configuration resolution.

Resolves settings and the enabled-tools map into the immutable
:class:`_PipelineConfig` the passes run under: feature flags, the two
:class:`ModelLane` call surfaces (writer + agent), length-guard config, and the
dynamic tool-schema overrides that stay byte-identical across all passes (so the
LLM's KV cache is not busted).

Imports the pass modules (length guard, director/editor overrides) — which is
why the dependency-free predicates live in ``predicates.py`` rather than here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..core import ChatMessage, Macros
from ..database.models import PhraseGroup
from ..inference import (
    CachedBase,
    LLMClient,
    build_direction_note_tool,
    enabled_schemas,
    writer_tool_incompatibility,
)
from ..workflows import RegistrySnapshot
from ..workflows.enablement import disabled_workflow_tool_names
from .passes.director import build_direct_scene_override
from .passes.editor import _feedback_active, build_feedback_override
from .passes.editor.length_guard import (
    LengthGuard,
    apply_length_guard_tools,
    resolve_length_guard,
)
from .predicates import agent_enabled, direction_note_recording_active, is_dual_model
from .state import ModelLane, WriterToolPolicy, _PipelineConfig

logger = logging.getLogger(__name__)


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
    registry: RegistrySnapshot | None = None,
) -> _PipelineConfig:
    """Build the immutable per-turn config.

    Resolves feature flags (audit, length guard, per-pass reasoning), the
    per-lane tool schema sets, the one active Writer tool, and the writer and
    agent lanes. Called once per turn by ``_run_pipeline``.
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

    dual_model = is_dual_model(agent_client)
    writer_text_mode = getattr(client, "completion_mode", "chat") == "text"

    agent_tool_schemas = tuple(enabled_schemas(enabled_tools, schema_overrides))
    # Resolved from the captured snapshot, never from the live registry, and
    # deliberately *not* gated on ``agent_on``: agent enablement decides which
    # Director/Editor tools exist, and letting it also decide Writer
    # eligibility would mean turning the Director off silently removed a tool
    # the user selected for the Writer.
    policy = _resolve_writer_tool_policy(settings, registry, text_mode=writer_text_mode)
    writer_tool_schemas = (policy.binding.spec.provider_schema(),) if policy.binding is not None else ()

    # Single model: one deterministic union, one ``CachedBase``, one object.
    # Dual model: each lane sees only its own schemas -- the Writer never
    # receives agent-only tools it is barred from calling, and the agent never
    # receives a Writer tool absent from its own transcripts.
    writer_tools = writer_tool_schemas if dual_model else _union_tool_schemas(agent_tool_schemas, writer_tool_schemas)
    writer_lane = ModelLane(
        client=client,
        base=CachedBase(
            prefix=tuple(prefix),
            tools=writer_tools,
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
                tools=agent_tool_schemas,
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
        agent_tool_schemas=agent_tool_schemas,
        writer_tool_schemas=writer_tool_schemas,
        writer_tool_policy=policy,
        writer_text_mode=writer_text_mode,
        writer_lane=writer_lane,
        agent_lane=agent_lane,
    )


def _union_tool_schemas(agent: Sequence[dict], writer: Sequence[dict]) -> tuple[dict, ...]:
    """The single-model blob: agent schemas first, then Writer schemas.

    Order is fixed rather than sorted, and that is the point. Agent schemas keep
    their registry order so an install with no Writer tool produces exactly the
    bytes it produced before this feature existed; a selected Writer schema is
    appended, so selecting one perturbs the tail of the blob rather than
    reshuffling the whole prefix. A collision is impossible by construction --
    the derived Writer namespace is prefixed -- but it is checked rather than
    assumed, because the failure mode is a provider silently binding one name to
    two schemas.
    """
    names = {entry["function"]["name"] for entry in agent if isinstance(entry.get("function"), dict)}
    merged = list(agent)
    for entry in writer:
        name = entry.get("function", {}).get("name")
        if name in names:
            logger.error("Writer tool %r collides with an agent tool name; dropping it from this turn's blob", name)
            continue
        names.add(name)
        merged.append(entry)
    return tuple(merged)


def _resolve_writer_tool_policy(
    settings: Mapping[str, Any],
    registry: RegistrySnapshot | None,
    *,
    text_mode: bool,
) -> WriterToolPolicy:
    """Resolve the one active Writer tool for this turn, or none.

    Three independent conditions, and each produces a *different* outcome:

    * no selection, or a selection that is no longer eligible -- nothing is
      active and there is nothing to say, because the manager already explains
      why beside the package;
    * an eligible selection on an endpoint that cannot host it -- nothing is
      active and a diagnostic is recorded for the host surfaces that explain why
      the selected tool did not appear;
    * an eligible selection on a capable endpoint -- the binding, its bounded
      description, and a schema-derived parameter summary.

    The endpoint case necessarily produces a different tool blob from a capable
    endpoint's. That is accepted: the model/endpoint identity already separates
    those cache lineages, and advertising a tool that cannot execute safely
    would spend a turn to refuse it.
    """
    binding = registry.writer_tool(registry.active_writer_tool) if registry is not None else None
    if binding is None:
        return WriterToolPolicy()
    diagnostic = writer_tool_incompatibility(
        settings.get("endpoint_url", "") or "",
        settings.get("model_name", "") or "",
        "text" if text_mode else (settings.get("completion_mode", "chat") or "chat"),
    )
    if diagnostic:
        return WriterToolPolicy(diagnostic=diagnostic)
    function = binding.spec.schema.get("function", {})
    return WriterToolPolicy(
        binding=binding,
        description=str(function.get("description", "")),
        parameter_summary=_parameter_summary(function.get("parameters")),
    )


def _parameter_summary(parameters: Any) -> str:
    """A one-line ``name (required)`` list derived from the compiled schema.

    Derived rather than package-authored: the model already receives the schema
    itself, and the OOC block's job is to make the *call decision* legible, not
    to give a package a second, unbounded place to put prose in the prompt.
    """
    if not isinstance(parameters, Mapping):
        return ""
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping) or not properties:
        return ""
    required = set(parameters.get("required") or ())
    return ", ".join(f"{name}{' (required)' if name in required else ''}" for name in properties)


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
) -> dict:
    """Build the dynamic tool-schema overrides shared across all cached calls.

    Mutates *enabled_tools* in place to enable ``give_feedback`` when the feedback
    step is active, ``record_direction_note`` when the direction-note step is, and
    ``select_lorebook`` when agentic lorebook is active. Returns a ``schema_overrides``
    dict (``direct_scene`` and optionally ``give_feedback``/``record_direction_note``)
    held byte-stable across every cached call in a turn so the LLM's KV cache is not
    busted. (``select_lorebook`` needs no override -- its schema is fixed, so enabling
    it lets ``enabled_schemas`` emit the registry schema into the shared blob.)

    Called by ``_prepare_turn``.
    """
    writer_fragments, feedback_fragments, direction_note_fragments = _split_interactive_fragments(interactive_fragments)
    direct_scene = build_direct_scene_override(writer_fragments)
    # Per-fragment mode fills one field per call, so requiredness on the shared blob
    # is meaningless -- and a non-empty `required` contradicts the "Fill ONLY X, leave
    # others empty" step prompt, which confuses the reasoning pass on endpoints that
    # can't grammar-narrow the call (no structured-tool-calls profile). Drop it.
    if bool(settings.get("director_individual_fragments", 0)):
        direct_scene["function"]["parameters"]["required"] = []
    overrides: dict = {"direct_scene": direct_scene}
    if agentic_lorebook:
        enabled_tools["select_lorebook"] = True
    if _feedback_active(settings, feedback_fragments, agent_on=agent_enabled(settings)):
        overrides["give_feedback"] = build_feedback_override(feedback_fragments)
        enabled_tools["give_feedback"] = True
    if direction_note_recording_active(settings, direction_note_fragments, agent_on=agent_enabled(settings)):
        overrides["record_direction_note"] = build_direction_note_tool(direction_note_fragments)
        enabled_tools["record_direction_note"] = True
    return overrides
