"""Workflow subsystem package.

Public surface re-exported from this module:
  - ``Workflow``, ``Subscription``, ``HookType``, ``ToolNameCollision``,
    ``WorkflowMandateError``
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
    HookType,
    OnDemandCtx,
    PostCtx,
    PreCtx,
    RegenCtx,
    RerollGenCtx,
    ToolSpec,
    _readonly,
)
from .registry import (
    Subscription,
    ToolNameCollision,
    Workflow,
    WorkflowMandateError,
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
    register_workflow,
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
    post_pipeline as _tts_post_pipeline,
    regenerate as _tts_regenerate,
    reroll_gen as _tts_reroll_gen,
)


__all__ = [
    "HookType",
    "OnDemandCtx",
    "PostCtx",
    "PreCtx",
    "RegenCtx",
    "RerollGenCtx",
    "Subscription",
    "ToolNameCollision",
    "ToolSpec",
    "Workflow",
    "WorkflowMandateError",
    "_readonly",
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
    "register_workflow",
    "set_workflow_character_state",
    "set_workflow_config",
    "set_workflow_message_state",
    "set_workflow_state",
    "subscribe",
    "workflow_has_hook",
]


register_workflow(tts_workflow)
subscribe(tts_workflow.id, HookType.POST_PIPELINE, _tts_post_pipeline)
subscribe(tts_workflow.id, HookType.ON_DEMAND, _tts_on_demand)
subscribe(tts_workflow.id, HookType.REGENERATE, _tts_regenerate)
subscribe(tts_workflow.id, HookType.REROLL_GEN, _tts_reroll_gen)


finalize_registry()
