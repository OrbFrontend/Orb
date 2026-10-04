"""Bridge pipeline turns to secondary-workflow hooks."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..core import ChatMessage, workflow_character_state_lock, workflow_state_lock
from ..inference import AbortToken, KVCacheTracker, LLMClient, until_aborted
from ..prompting.tool_catalog import has_tool
from ..workflows import (
    EV_ATTACH_ARTIFACT,
    EV_DRAFT_REPLACED,
    EV_ENABLE_TOOLS,
    EV_SET_MESSAGE_STATE,
    EV_SYSTEM_PROMPT,
    HookType,
    PostCtx,
    PreCtx,
    get_workflow,
    iter_subscriptions,
    public_event_error,
    readonly_view,
)
from ..workflows.enablement import effective_workflow_enabled
from ..workflows.errors import WorkflowUserFacingError
from .failures import describe_failure

logger = logging.getLogger(__name__)


def _public_hook_event(ev: object, *, hook_type: str, workflow_id: str) -> dict | None:
    """Return a valid public SSE event, or log and drop malformed output.

    Control events are consumed before this boundary. Anything left must use the public ``{"event": <non-empty str>, ...}``
    shape; accepting arbitrary objects here merely defers the failure to the SSE adapter. Shape validation lives in
    ``workflows.contracts.public_event_error`` so this bridge and the API on-demand SSE encoder enforce one definition of a
    public event.
    """
    reason = public_event_error(ev)
    if reason is not None:
        logger.warning("%s hook %r yielded an invalid public event (%s); dropping", hook_type, workflow_id, reason)
        return None
    return cast(dict, ev)


def _hook_warning(exc: Exception, workflow_id: str) -> dict | None:
    """Return a non-terminal warning for WorkflowUserFacingError; defects stay log-only.

    Hook failures do not invalidate prose. Do not emit error here: SSE reserves it for terminal failure.
    """
    if not isinstance(exc, WorkflowUserFacingError):
        return None
    payload = describe_failure(exc)
    payload["headline"] = f"Workflow {workflow_id} failed."
    payload["workflow_id"] = workflow_id
    return {"event": "warning", "data": payload}


def _hook_events(events: AsyncIterator[Any], abort: AbortToken | None) -> AsyncIterator[Any]:
    """A hook's events, interrupted by a stop when the turn has an abort token."""
    return events if abort is None else until_aborted(events, abort)


@dataclass(slots=True)
class PostPipelineResult:
    """Final value of :func:`run_post_pipeline`: the (possibly rewritten) draft
    plus any attachments and per-message state staged for persistence."""

    draft: str
    staged_attachments: list[dict]
    staged_message_state: dict[str, dict]


async def run_post_pipeline(
    *,
    draft: str,
    conversation_id: str | None,
    character_id: str | None,
    card: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]] | None,
    effective_msg: str,
    director_output: dict,
    settings: Mapping[str, Any],
    prefix: list[ChatMessage],
    enabled_tools: Mapping[str, bool],
    turn_scratch: dict,
    client: LLMClient,
    kv_tracker: KVCacheTracker,
    schema_overrides: Mapping[str, dict],
    agent_client: LLMClient | None = None,
    agent_model_name: str = "",
    post_workflow_ids: Collection[str] | None = None,
    on_accepted: Callable[[PostPipelineResult], None] | None = None,
) -> AsyncIterator[dict | PostPipelineResult]:
    """Run selected POST_PIPELINE hooks over the post-Editor draft.

    Yield public events and a final PostPipelineResult; log and skip hook failures. post_workflow_ids restricts dispatch. Stop
    interrupts the active hook and starts no more, dropping later events but retaining accepted drafts/artifacts/ state.
    on_accepted receives each growing result for cancellation-safe saves.
    """
    staged_attachments: list[dict] = []
    staged_message_state: dict[str, dict] = {}
    abort: AbortToken | None = getattr(client, "abort_token", None)

    def accepted() -> None:
        if on_accepted is not None:
            on_accepted(PostPipelineResult(draft, list(staged_attachments), dict(staged_message_state)))

    for sub in iter_subscriptions(HookType.POST_PIPELINE):
        if post_workflow_ids is not None and sub.workflow_id not in post_workflow_ids:
            continue
        if not effective_workflow_enabled(sub.workflow_id, settings):
            logger.info("workflow %r post-pipeline hook suspended (disabled)", sub.workflow_id)
            continue
        if abort is not None and abort.is_aborted:
            break
        replaced_this_hook = False
        # Serialize same-(cid, workflow_id) writers against concurrent
        # /trigger calls and any other in-flight pipeline that reaches this
        # hook on the same conversation. Different workflows on the same
        # conversation keep distinct lock keys, so they still run in parallel.
        async with (
            workflow_state_lock(conversation_id or "", sub.workflow_id),
            workflow_character_state_lock(character_id or "", sub.workflow_id),
        ):
            # A stop that arrived while another writer held the lock starts nothing.
            if abort is not None and abort.is_aborted:
                break
            try:
                post_ctx = PostCtx(
                    conversation_id=conversation_id or "",
                    history=readonly_view(history or []),
                    draft=draft,
                    effective_msg=effective_msg,
                    director_output=readonly_view(director_output),
                    settings=readonly_view(settings),
                    prefix=readonly_view(prefix),
                    enabled_tools=readonly_view(enabled_tools),
                    turn_scratch=turn_scratch,
                    client=client,
                    kv_tracker=kv_tracker,
                    schema_overrides=readonly_view(schema_overrides),
                    character_id=character_id,
                    character=readonly_view(card),
                    # The execution target for a forced Agent call: in dual-model mode the Writer is a different endpoint.
                    agent_client=agent_client if agent_client is not None else client,
                    agent_model_name=agent_model_name,
                )
                async for ev in _hook_events(sub.callable(post_ctx), abort):
                    t = ev.get("type") if isinstance(ev, dict) else None
                    if t == EV_DRAFT_REPLACED:
                        if replaced_this_hook:
                            logger.warning("post_pipeline hook %r yielded a second draft_replaced; ignoring", sub.workflow_id)
                            continue
                        new_draft = ev.get("draft")
                        if not isinstance(new_draft, str) or new_draft == draft:
                            logger.warning(
                                "post_pipeline hook %r yielded malformed draft_replaced "
                                "(draft type=%s, unchanged=%s); ignoring",
                                sub.workflow_id,
                                type(new_draft).__name__,
                                new_draft == draft,
                            )
                            continue
                        draft = new_draft
                        replaced_this_hook = True
                        accepted()
                        yield {"event": "writer_rewrite", "data": {"refined_text": draft}}
                        continue
                    if t == EV_ATTACH_ARTIFACT:
                        # Only workflows with produces_artifacts=True may persist attachments.
                        w = get_workflow(sub.workflow_id)
                        if not (w and w.produces_artifacts):
                            logger.warning(
                                "post_pipeline hook %r yielded attach_artifact but "
                                "workflow does not declare produces_artifacts=True; "
                                "dropping entry",
                                sub.workflow_id,
                            )
                            continue
                        staged = _stage_workflow_attachment(
                            ev.get("attachment") if isinstance(ev, dict) else None, sub.workflow_id
                        )
                        if staged is not None:
                            staged_attachments.append(staged)
                            accepted()
                        continue
                    if t == EV_SET_MESSAGE_STATE:
                        # Written in _persist_result once the assistant row id is known.
                        state = ev.get("state") if isinstance(ev, dict) else None
                        if not isinstance(state, dict):
                            logger.warning(
                                "post_pipeline hook %r yielded set_message_state with non-dict state (type=%s); ignoring",
                                sub.workflow_id,
                                type(state).__name__,
                            )
                            continue
                        staged_message_state[sub.workflow_id] = state
                        accepted()
                        continue
                    # A dict carrying a "type" key is a control event; if it matched no known branch above it is malformed (e.g.
                    # a typo'd type, or a leaked sub-generator terminal). Drop it rather than letting it fall through and be
                    # emitted to the client as a stray SSE event.
                    if t is not None:
                        logger.warning(
                            "post_pipeline hook %r yielded unknown control event type %r; dropping", sub.workflow_id, t
                        )
                        continue
                    public_event = _public_hook_event(ev, hook_type="post_pipeline", workflow_id=sub.workflow_id)
                    if public_event is not None:
                        yield public_event
            except Exception as e:
                logger.exception("post_pipeline hook %r failed", sub.workflow_id)
                warning = _hook_warning(e, sub.workflow_id)
                if warning is not None:
                    yield warning

    yield PostPipelineResult(draft, staged_attachments, staged_message_state)


def _stage_workflow_attachment(att: object, workflow_id: str) -> dict | None:
    """Validate and normalize a workflow ``attach_artifact`` entry.

    Returns a bytes-only dict ready for ``add_message``, or ``None`` if validation fails (logged as a warning). Never raises —
    bad workflow output must not crash the turn.
    """
    if not isinstance(att, dict):
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact with non-dict attachment (type=%s); ignoring",
            workflow_id,
            type(att).__name__,
        )
        return None

    expected_source = f"workflow:{workflow_id}"
    filename = att.get("filename")
    mime = att.get("mime")
    has_data = "data" in att
    has_path = "path" in att
    annotation_present = "annotation" in att
    raw_annotation = att.get("annotation")

    valid = (
        isinstance(filename, str)
        and isinstance(mime, str)
        and (has_data != has_path)
        and ((not has_data) or isinstance(att["data"], (bytes, bytearray)))
        and ((not has_path) or isinstance(att["path"], str))
        and ((not annotation_present) or raw_annotation is None or isinstance(raw_annotation, str))
        and att.get("source") == expected_source
        and att.get("workflow_id") == workflow_id
    )
    if not valid:
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact failing validation "
            "(filename/mime/data-xor-path/source/workflow_id/annotation); ignoring entry",
            workflow_id,
        )
        return None

    out = dict(att)
    # Whitespace-only annotation collapses to None ("no LLM-visible footprint").
    if isinstance(raw_annotation, str) and not raw_annotation.strip():
        out["annotation"] = None

    raw_cm = out.get("consumption_metadata")
    if raw_cm is not None and not isinstance(raw_cm, dict):
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact with non-dict consumption_metadata "
            "(filename=%r, type=%s); coercing to None",
            workflow_id,
            filename,
            type(raw_cm).__name__,
        )
        out["consumption_metadata"] = None

    if has_path:
        try:
            with open(att["path"], "rb") as f:
                data_bytes = f.read()
        except OSError as e:
            logger.warning(
                "post_pipeline hook %r yielded attach_artifact with path=%r that failed to read (%s); dropping entry",
                workflow_id,
                att["path"],
                e,
            )
            return None
        out.pop("path", None)
        out["data"] = data_bytes
    else:
        out["data"] = bytes(att["data"])

    if not out.get("data"):
        logger.warning(
            "post_pipeline hook %r yielded attach_artifact with empty data (filename=%r); dropping entry", workflow_id, filename
        )
        return None

    return out


async def iterate_pre_pipeline_hooks(
    *,
    conversation_id: str,
    character_id: str | None = None,
    card: Mapping[str, Any] | None = None,
    history: Sequence[Mapping[str, Any]],
    last_user_message: str,
    settings: Mapping[str, Any],
    prefix_base: list[ChatMessage],
    enabled_tools_pre_merge: Mapping[str, bool],
    turn_scratch: dict,
    client,
    kv_tracker,
    schema_overrides: Mapping[str, dict],
    accumulators: dict,
) -> AsyncIterator[dict]:
    """Run PRE_PIPELINE hooks, forwarding events and merging tools/system extras.

    Prepopulate accumulators with merged_enabled_tools and extras. Log and skip
    hook failures; Stop interrupts the current hook and starts no more.
    """
    abort: AbortToken | None = getattr(client, "abort_token", None)
    for sub in iter_subscriptions(HookType.PRE_PIPELINE):
        if not effective_workflow_enabled(sub.workflow_id, settings):
            logger.info("workflow %r pre-pipeline hook suspended (disabled)", sub.workflow_id)
            continue
        if abort is not None and abort.is_aborted:
            break
        # Lock held for the hook's full lifetime to keep workflow_state RMW atomic.
        async with (
            workflow_state_lock(conversation_id, sub.workflow_id),
            workflow_character_state_lock(character_id or "", sub.workflow_id),
        ):
            if abort is not None and abort.is_aborted:
                break
            try:
                pre_ctx = PreCtx(
                    conversation_id=conversation_id,
                    history=readonly_view(history),
                    last_user_message=last_user_message,
                    settings=readonly_view(settings),
                    prefix=readonly_view(prefix_base),
                    enabled_tools_pre_merge=readonly_view(enabled_tools_pre_merge),
                    turn_scratch=turn_scratch,
                    client=client,
                    kv_tracker=kv_tracker,
                    schema_overrides=readonly_view(schema_overrides),
                    character_id=character_id,
                    character=readonly_view(card),
                )
                async for ev in _hook_events(sub.callable(pre_ctx), abort):
                    t = ev.get("type") if isinstance(ev, dict) else None
                    if t == EV_ENABLE_TOOLS:
                        tools = ev.get("tools")
                        if isinstance(tools, (set, frozenset)):
                            items = ((n, True) for n in tools)
                        elif isinstance(tools, dict):
                            items = tools.items()
                        else:
                            logger.warning(
                                "pre_pipeline hook %r yielded enable_tools with invalid tools payload (type=%s); ignoring",
                                sub.workflow_id,
                                type(tools).__name__,
                            )
                            continue
                        for name, val in items:
                            if val is not True:
                                logger.warning(
                                    "workflow %r yielded enable_tools %r=%r; only True is honored, entry dropped",
                                    sub.workflow_id,
                                    name,
                                    val,
                                )
                                continue
                            if not has_tool(name):
                                logger.warning("workflow %r enabled unregistered tool %r; dropping", sub.workflow_id, name)
                                continue
                            accumulators["merged_enabled_tools"][name] = True
                        continue
                    if t == EV_SYSTEM_PROMPT:
                        block = ev.get("block")
                        if not isinstance(block, str) or not block.strip():
                            logger.warning(
                                "pre_pipeline hook %r yielded empty/whitespace-only system_prompt; ignoring", sub.workflow_id
                            )
                            continue
                        accumulators["extras"].append(block)
                        continue
                    # Unknown control event ("type" present but unmatched): drop it
                    # instead of leaking it through as a stray SSE event.
                    if t is not None:
                        logger.warning(
                            "pre_pipeline hook %r yielded unknown control event type %r; dropping", sub.workflow_id, t
                        )
                        continue
                    public_event = _public_hook_event(ev, hook_type="pre_pipeline", workflow_id=sub.workflow_id)
                    if public_event is not None:
                        yield public_event
            except Exception as e:
                logger.exception("pre_pipeline hook %r failed", sub.workflow_id)
                warning = _hook_warning(e, sub.workflow_id)
                if warning is not None:
                    yield warning
