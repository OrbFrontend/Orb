"""Workflow contracts, registry, storage, and plug-in discovery."""

from __future__ import annotations

from pathlib import Path

from ..core.settings import Settings
from ..database import get_settings, set_workflow_enabled
from . import gpu_swap_host, prose_rewriter_host
from .contracts import (
    EV_ATTACH_ARTIFACT,
    EV_DRAFT_REPLACED,
    EV_ENABLE_TOOLS,
    EV_SET_MESSAGE_STATE,
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
    StandaloneCtx,
    StandaloneReference,
    StandaloneResult,
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
# adapter binds it here. Priority -10 runs it after Format Consistency (-20) and
# before TTS (0), so artifacts use the final prose. Its workflow toggle turns it
# on for manual and automatic rewrites; its ``automatic`` config gates turns.
subscribe(prose_rewriter_host.FEATURE, HookType.POST_PIPELINE, prose_rewriter_host.post_pipeline, priority=-10)

finalize_registry()


async def set_workflow_toggle(workflow_id: str, enabled: bool) -> None:
    """Store one workflow's on/off flag, then run the runtime effects of the change."""
    before = await get_settings()
    await set_workflow_enabled(workflow_id, enabled)
    await live_settings_changed(before)


async def live_settings_changed(before: Settings) -> None:
    """Run the runtime effects of a write to the live settings, given the settings read before it. Every writer that can change
    a switch with an effect (the settings and workflow routes, a profile switch, reset, a Backups apply) calls this after.

    The rewriter's model can hold gigabytes of VRAM: once no mode uses it, it is freed now rather than after the idle timeout,
    and once a mode does, it warms. The GPU swap plan reads the lanes, workflow switches and Image Generation's config, so its
    cached plan is dropped: a render right after the change must not use the old one.
    """
    gpu_swap_host.invalidate()
    await prose_rewriter_host.settings_changed(before, await get_settings())
