"""Workflow contracts, registry, storage, and plug-in discovery."""

from __future__ import annotations

from pathlib import Path

from . import prose_rewriter_host
from .contracts import (
    EV_ATTACH_ARTIFACT,
    EV_DRAFT_REPLACED,
    EV_ENABLE_TOOLS,
    EV_SET_MESSAGE_STATE,
    EV_SYSTEM_PROMPT,
    AttachArtifactEvent,
    DraftReplacedEvent,
    EnableToolsEvent,
    ExportCtx,
    ExportedFile,
    HookType,
    OnDemandCtx,
    OnDemandResult,
    PostCtx,
    PostEvent,
    PreCtx,
    PreEvent,
    PublicEvent,
    QueryCtx,
    RegenCtx,
    RerollGenCtx,
    SetMessageStateEvent,
    SystemPromptEvent,
    ToolSpec,
    UploadCtx,
    WorkflowEventStream,
    public_event_error,
    readonly_view,
)
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
    register_plugins,
    register_workflow,
    set_workflow_character_state,
    set_workflow_config,
    set_workflow_message_state,
    set_workflow_state,
    subscribe,
    workflow_has_hook,
)

# Every package under this directory is a plug-in that declares its Workflow and
# hook subscriptions as WORKFLOW; they register in package-name order.
register_plugins(__name__, Path(__file__).parent)

# The rewriter's post hook runs the local model, below the toolkit, so its host
# adapter binds it here. Priority -20 runs it before Format Consistency (-10) and
# TTS (0), so both act on the rewritten draft. Its workflow toggle turns it on for
# manual and automatic rewrites; its ``automatic`` config gates turns.
subscribe(prose_rewriter_host.FEATURE, HookType.POST_PIPELINE, prose_rewriter_host.post_pipeline, priority=-20)

finalize_registry()
