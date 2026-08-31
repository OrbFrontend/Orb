"""Workflow contracts, registry, storage, and built-in workflow bindings."""

from __future__ import annotations

from .contracts import (
    EV_ATTACH_ARTIFACT,
    EV_DRAFT_REPLACED,
    EV_ENABLE_TOOLS,
    EV_SET_MESSAGE_STATE,
    EV_SYSTEM_PROMPT,
    HookType,
    OnDemandCtx,
    OnDemandResult,
    PostCtx,
    PreCtx,
    QueryCtx,
    RegenCtx,
    RerollGenCtx,
    ToolSpec,
    WorkflowEventStream,
    _readonly,
    public_event_error,
)
from .format_consistency import format_consistency_workflow
from .format_consistency.hooks import (
    post_pipeline as _fc_post_pipeline,
)
from .image_gen import image_gen_workflow
from .image_gen.hooks import on_demand as _image_gen_on_demand
from .image_gen.hooks import regenerate as _image_gen_regenerate
from .image_gen.hooks import reroll_gen as _image_gen_reroll_gen
from .image_gen.queries import query as _image_gen_query
from .registry import (
    Subscription,
    ToolNameCollision,
    Workflow,
    WorkflowDeclarationError,
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
    "EV_ENABLE_TOOLS",
    "EV_SET_MESSAGE_STATE",
    "EV_SYSTEM_PROMPT",
    "HookType",
    "OnDemandCtx",
    "OnDemandResult",
    "PostCtx",
    "PreCtx",
    "QueryCtx",
    "RegenCtx",
    "RerollGenCtx",
    "Subscription",
    "ToolNameCollision",
    "ToolSpec",
    "Workflow",
    "WorkflowDeclarationError",
    "WorkflowEventStream",
    "WorkflowMandateError",
    "_readonly",
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
subscribe(tts_workflow.id, HookType.QUERY, _tts_query)
subscribe(tts_workflow.id, HookType.REGENERATE, _tts_regenerate)
subscribe(tts_workflow.id, HookType.REROLL_GEN, _tts_reroll_gen)

# Negative priority makes the deterministic markup normalizer run before TTS's
# post hook (priority 0), so TTS — and any future artifact hook — synthesizes
# from the normalized text rather than the raw draft.
register_workflow(format_consistency_workflow)
subscribe(format_consistency_workflow.id, HookType.POST_PIPELINE, _fc_post_pipeline, priority=-10)

register_workflow(image_gen_workflow)
subscribe(image_gen_workflow.id, HookType.ON_DEMAND, _image_gen_on_demand)
subscribe(image_gen_workflow.id, HookType.QUERY, _image_gen_query)
subscribe(image_gen_workflow.id, HookType.REGENERATE, _image_gen_regenerate)
subscribe(image_gen_workflow.id, HookType.REROLL_GEN, _image_gen_reroll_gen)


finalize_registry()
