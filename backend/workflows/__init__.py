"""Workflow subsystem package.

Public surface re-exported from this module:
  - ``Workflow``, ``Subscription``, ``HookType``, ``ToolNameCollision``,
    ``WorkflowDeclarationError``, ``WorkflowMandateError``
  - ``register_workflow``, ``subscribe``, ``iter_subscriptions``,
    ``get_subscription``, ``workflow_has_hook``, ``list_workflows``,
    ``get_workflow``, ``finalize_registry``
  - ``ToolSpec``, ``PreCtx``, ``PostCtx``, ``OnDemandCtx``, ``RegenCtx``,
    ``RerollGenCtx``
  - per-workflow storage wrappers and ``overlay_enable_tools``

Workflow authors should import day-to-day helpers from
``backend.workflows.toolkit`` instead -- that module is the
stable import surface for LLM client, prompt assembly, DB readers, and
the forced-call helper. This module is the registration / typing
surface.

First-party workflows live under ``backend/workflows/`` and
are wired in here above the ``finalize_registry()`` call at the bottom
of this file: each workflow's metadata is registered via
``register_workflow``, then each of its hooks is attached via
``subscribe`` with a per-hook priority. Import-time ordering of those
calls determines the registry's iteration order and the manifest order
surfaced to the frontend. The final ``finalize_registry()`` call
validates that every ``produces_artifacts=True`` workflow has both
``REGENERATE`` and ``REROLL_GEN`` subscriptions; a violation raises
``WorkflowMandateError`` at import time.
"""

from __future__ import annotations

from .contracts import (
    EV_ATTACH_ARTIFACT,
    EV_CONTEXT_BLOCK,
    EV_DRAFT_REPLACED,
    EV_ENABLE_TOOLS,
    EV_SET_MESSAGE_STATE,
    EV_SYSTEM_PROMPT,
    FrontendKind,
    HookStage,
    HookType,
    LoadStatus,
    OnDemandCtx,
    OnDemandResult,
    PostCtx,
    PreCtx,
    QueryCtx,
    RegenCtx,
    RerollGenCtx,
    ToolSpec,
    WorkflowEventStream,
    WorkflowSource,
    WriterToolBinding,
    WriterToolRequest,
    _readonly,
    public_event_error,
)
from .format_consistency import format_consistency_workflow
from .format_consistency.hooks import (
    post_pipeline as _fc_post_pipeline,
)
from .fragment_types import (
    BUILTIN_FRAGMENT_TYPES,
    MAX_EXTENSION_FRAGMENT_INSTANCES_PER_TURN,
    MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET,
    FragmentReducerBudget,
    FragmentReduceRequest,
    FragmentTypeDefinition,
    FragmentTypeError,
    FragmentTypeInstance,
)
from .image_gen import image_gen_workflow
from .image_gen.hooks import on_demand as _image_gen_on_demand
from .image_gen.hooks import query as _image_gen_query
from .image_gen.hooks import regenerate as _image_gen_regenerate
from .image_gen.hooks import reroll_gen as _image_gen_reroll_gen
from .registry import (
    RegistrySnapshot,
    Subscription,
    ToolNameCollision,
    Workflow,
    WorkflowDeclarationError,
    WorkflowMandateError,
    bump_generation,
    current_snapshot,
    finalize_registry,
    get_subscription,
    get_workflow,
    get_workflow_character_state,
    get_workflow_config,
    get_workflow_message_state,
    get_workflow_state,
    iter_subscriptions,
    list_workflows,
    overlay_enable_tools,
    publish_community_overlay,
    register_workflow,
    runtime_generation,
    set_workflow_character_state,
    set_workflow_config,
    set_workflow_message_state,
    set_workflow_state,
    subscribe,
    workflow_has_hook,
)
from .tts import tts_workflow
from .tts.hooks import (
    on_demand as _tts_on_demand,
)
from .tts.hooks import (
    post_pipeline as _tts_post_pipeline,
)
from .tts.hooks import (
    query as _tts_query,
)
from .tts.hooks import (
    regenerate as _tts_regenerate,
)
from .tts.hooks import (
    reroll_gen as _tts_reroll_gen,
)

__all__ = [
    "EV_ATTACH_ARTIFACT",
    "EV_DRAFT_REPLACED",
    "EV_CONTEXT_BLOCK",
    "EV_ENABLE_TOOLS",
    "EV_SET_MESSAGE_STATE",
    "EV_SYSTEM_PROMPT",
    "FrontendKind",
    "BUILTIN_FRAGMENT_TYPES",
    "FragmentReduceRequest",
    "FragmentReducerBudget",
    "FragmentTypeDefinition",
    "FragmentTypeError",
    "FragmentTypeInstance",
    "MAX_EXTENSION_FRAGMENT_INSTANCES_PER_TURN",
    "MAX_FRAGMENT_CONTEXT_BYTES_PER_TARGET",
    "HookStage",
    "HookType",
    "LoadStatus",
    "OnDemandCtx",
    "OnDemandResult",
    "PostCtx",
    "PreCtx",
    "QueryCtx",
    "RegenCtx",
    "RegistrySnapshot",
    "RerollGenCtx",
    "Subscription",
    "ToolNameCollision",
    "ToolSpec",
    "Workflow",
    "WorkflowDeclarationError",
    "WorkflowEventStream",
    "WorkflowMandateError",
    "WorkflowSource",
    "WriterToolBinding",
    "WriterToolRequest",
    "_readonly",
    "bump_generation",
    "current_snapshot",
    "public_event_error",
    "finalize_registry",
    "get_subscription",
    "get_workflow",
    "get_workflow_character_state",
    "get_workflow_config",
    "get_workflow_message_state",
    "get_workflow_state",
    "iter_subscriptions",
    "list_workflows",
    "overlay_enable_tools",
    "publish_community_overlay",
    "register_workflow",
    "runtime_generation",
    "set_workflow_character_state",
    "set_workflow_config",
    "set_workflow_message_state",
    "set_workflow_state",
    "subscribe",
    "workflow_has_hook",
]


# TTS consumes the finished draft and never rewrites it, so it is an observer:
# it is guaranteed to synthesize the exact text the user will read, after every
# transform (built-in or community) has run.
register_workflow(tts_workflow)
subscribe(tts_workflow.id, HookType.POST_PIPELINE, _tts_post_pipeline, stage=HookStage.OBSERVE)
subscribe(tts_workflow.id, HookType.ON_DEMAND, _tts_on_demand)
subscribe(tts_workflow.id, HookType.QUERY, _tts_query)
subscribe(tts_workflow.id, HookType.REGENERATE, _tts_regenerate)
subscribe(tts_workflow.id, HookType.REROLL_GEN, _tts_reroll_gen)

# The deterministic markup normalizer rewrites the draft, so it is a transform.
# Its negative priority keeps it first among transforms; the transform/observe
# split is what actually guarantees TTS sees its output (priority alone would
# not, once a community transform with a lower priority exists).
register_workflow(format_consistency_workflow)
subscribe(
    format_consistency_workflow.id,
    HookType.POST_PIPELINE,
    _fc_post_pipeline,
    priority=-10,
    stage=HookStage.TRANSFORM,
)

register_workflow(image_gen_workflow)
subscribe(image_gen_workflow.id, HookType.ON_DEMAND, _image_gen_on_demand)
subscribe(image_gen_workflow.id, HookType.QUERY, _image_gen_query)
subscribe(image_gen_workflow.id, HookType.REGENERATE, _image_gen_regenerate)
subscribe(image_gen_workflow.id, HookType.REROLL_GEN, _image_gen_reroll_gen)


finalize_registry()
