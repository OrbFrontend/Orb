"""Secondary-workflow configuration and attachment routes."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from functools import partial
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, Response, UploadFile

from ...core import scrub_log, workflow_character_state_lock, workflow_config_lock, workflow_state_lock
from ...database import (
    conversation_attachment_ids,
    get_character_card,
    get_conversation,
    get_group_member,
    get_message_by_id,
    get_messages,
    get_messages_before,
    get_settings,
    get_workflow_attachment_by_id,
    get_workflow_attachment_bytes,
    get_workflow_attachment_meta,
    set_workflow_enabled,
)
from ...database.models import ConversationRow
from ...inference import agent_lane_from_settings, client_from_settings
from ...workflows import (
    ExportCtx,
    ExportedFile,
    HookType,
    OnDemandCtx,
    PublicEvent,
    QueryCtx,
    RegenCtx,
    RerollGenCtx,
    Subscription,
    UploadCtx,
    WorkflowEventStream,
    get_subscription,
    get_workflow,
    get_workflow_config,
    list_workflows,
    prose_rewriter_host,
    public_event_error,
    readonly_view,
    set_workflow_config,
)
from ...workflows.attachment_cache import (
    EVICTED_MARKER,
    RehydrateAlreadyDoneError,
    delete_workflow_attachments,
    insert_workflow_attachment,
    insert_workflow_attachments,
    insert_workflow_variant,
    project_rejected_attachment,
    record_access,
    rehydrate_attachment,
    set_active_sibling,
    validate_workflow_attachment_shape,
    variant_on_show,
)
from ...workflows.contracts import RerollGenHook, WorkflowHook
from ...workflows.enablement import effective_workflow_enabled
from ..deps import (
    attachment_content_response,
    committing_workflow_job,
    locked_attachment_group,
    require_conversation,
    start_workflow_job,
    stop_workflow_jobs,
    workflow_event_stream_response,
    workflow_group_in_flight,
)
from ..errors import API_PASSTHROUGH_ERRORS, http_failure
from ..schemas import WorkflowConfigUpdate, WorkflowEnabledUpdate

logger = logging.getLogger(__name__)

router = APIRouter()
_HookT = TypeVar("_HookT", bound=WorkflowHook)


async def _resolve_workflow_character(
    conv: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    *,
    target_message: Mapping[str, Any] | None = None,
    speaker_member_id: str | None = None,
) -> tuple[str | None, Mapping[str, Any] | None]:
    """Resolve the solo card or a group message/member's current card."""
    card_id = conv.get("character_card_id")
    if conv.get("kind", "solo") == "group":
        member_id = speaker_member_id
        if member_id is None and target_message is not None:
            member_id = target_message.get("speaker_member_id")
        if member_id is None:
            member_id = next(
                (message.get("speaker_member_id") for message in reversed(messages) if message.get("speaker_member_id")), None
            )
        if member_id:
            member = await get_group_member(str(member_id), conversation_id=str(conv["id"]))
            card_id = member.get("character_card_id") if member else None
        else:
            card_id = None
    card = await get_character_card(card_id) if card_id else None
    return card_id, card


def _gate_workflow_sub(
    sub: Subscription[_HookT] | None, wid: str, settings: Mapping[str, Any], *, action: str, detail: str
) -> Subscription[_HookT]:
    """Shared missing-handler / disabled gate, applied before any lock is taken.

    Returns the live subscription; raises 404 otherwise. A disabled workflow is indistinguishable from a missing handler to the
    caller (both 404); the log disambiguates server-side. Gating before the lock means a disabled-workflow request never
    contends for the same lock the live consumption routes hold.
    """
    if sub is None or not effective_workflow_enabled(wid, settings):
        if sub is not None:
            logger.info("workflow %r %s suspended (disabled)", scrub_log(wid), action)
        raise HTTPException(status_code=404, detail=detail)
    return sub


@contextmanager
def _hook_failures(label: str, wid: Any, aid: int | None = None, *, defect: str) -> Iterator[None]:
    """Map workflow hook failures to the API response shape."""

    def where() -> str:
        # Built on the failing path only, so the happy path pays nothing for it.
        target = f"{label} {scrub_log(wid)!r} failed"
        return target if aid is None else f"{target} for attachment {scrub_log(aid)!r}"

    try:
        yield
    except API_PASSTHROUGH_ERRORS:
        raise
    except Exception:
        logger.exception("%s", where())
        raise HTTPException(status_code=500, detail=defect) from None


@router.get("/api/workflows")
async def api_list_workflows():
    """Manifest the frontend reads once at boot to populate Secondary tabs and buttons."""
    return [
        {"id": w.id, "display_name": w.display_name, "config_schema": w.config_schema, "config_defaults": w.config_defaults}
        for w in list_workflows()
    ]


def _normalized(workflow_id: str, config: Any) -> Any:
    """Apply the workflow's own config normalizer, when it declares one.

    Both directions go through here so the panel edits, and then re-reads, the exact shape the workflow's hooks will use -- a
    value the normalizer clamps or an entry it drops must not survive in the UI as a setting that appears to have taken effect.
    """
    workflow = get_workflow(workflow_id)
    normalizer = workflow.config_normalizer if workflow else None
    return normalizer(config) if normalizer else config


@router.put("/api/workflows/{workflow_id}/config")
async def api_set_workflow_config(workflow_id: str, data: WorkflowConfigUpdate):
    """Persist a workflow's global config slot as a full replacement."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    config = _normalized(workflow_id, data.config)
    # Serialize the replacement with workflow code that updates the same slot via
    # a locked read-modify-write; a lock-free write here could be lost mid-RMW.
    async with workflow_config_lock():
        await set_workflow_config(workflow_id, config)
        effective = await get_workflow_config(workflow_id)
    logger.info("workflow %r config updated (%d keys)", scrub_log(workflow_id), len(data.config))
    return {"config": _normalized(workflow_id, effective)}


@router.get("/api/workflows/{workflow_id}/config")
async def api_get_workflow_config(workflow_id: str):
    """Return a workflow's effective config: persisted slot, else its defaults."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    return {"config": _normalized(workflow_id, await get_workflow_config(workflow_id))}


@router.post("/api/workflows/{workflow_id}/query")
async def api_query_workflow(workflow_id: str, body: dict = Body(default={})):  # noqa: B008
    """Run a workflow's conversation-free query hook."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    sub = get_subscription(workflow_id, HookType.QUERY)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} has no query handler")
    settings_snapshot = await get_settings()
    with _hook_failures("query hook", workflow_id, defect="Query handler raised; see server logs"):
        return await sub.callable(QueryCtx(settings=readonly_view(settings_snapshot)), body)


# Ceiling for one workflow upload, which the hook receives as bytes. Voice enrollment reads up to two minutes: generous enough
# for an uncompressed WAV of that length and far short of "someone dropped in a film".
MAX_WORKFLOW_UPLOAD = 25 * 1024 * 1024


@router.post("/api/characters/{card_id}/workflows/{workflow_id}/upload")
async def api_upload_workflow_file(card_id: str, workflow_id: str, request: Request, file: Annotated[UploadFile, File(...)]):
    """Hand one file for one character to a workflow's upload hook.

    The query string reaches the hook as ``params``. No lock is held while the
    hook runs; it takes the toolkit lock matching any state it rewrites.
    """
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    settings_snapshot = await get_settings()
    sub = _gate_workflow_sub(
        get_subscription(workflow_id, HookType.UPLOAD),
        workflow_id,
        settings_snapshot,
        action="upload",
        detail=f"Workflow {workflow_id!r} has no upload handler",
    )
    card = await get_character_card(card_id)
    if card is None:
        raise HTTPException(status_code=404, detail="Character card not found")
    data = await file.read(MAX_WORKFLOW_UPLOAD + 1)
    if len(data) > MAX_WORKFLOW_UPLOAD:
        raise HTTPException(status_code=400, detail=f"Uploads must be under {MAX_WORKFLOW_UPLOAD // (1024 * 1024)} MB")
    with _hook_failures("upload hook", workflow_id, defect="Upload handler raised; see server logs"):
        upload_ctx = UploadCtx(
            settings=readonly_view(settings_snapshot),
            character_id=card_id,
            character=readonly_view(card),
            filename=os.path.basename(file.filename or ""),
            data=data,
        )
        return await sub.callable(upload_ctx, dict(request.query_params))


@router.post("/api/workflows/{workflow_id}/enabled")
async def api_set_workflow_enabled(workflow_id: str, data: WorkflowEnabledUpdate):
    """Flip one workflow's on/off toggle and return the full decoded map.

    Ungated -- this is the control that re-enables a suspended workflow. A dedicated per-key route rather than PUT /settings
    because the latter does a full-column overwrite that would clobber a concurrent tab's flip of another workflow (the per-key
    json_set in set_workflow_enabled does not).
    """
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    await set_workflow_enabled(workflow_id, data.enabled)
    if workflow_id == prose_rewriter_host.FEATURE:
        # Its model can hold gigabytes of VRAM: free it now rather than after
        # the idle timeout, and warm it when the rewriter is switched back on.
        await prose_rewriter_host.on_enabled(data.enabled)
    settings = await get_settings()
    logger.info("workflow %r enabled=%s", scrub_log(workflow_id), data.enabled)
    return {"workflow_enabled": settings.get("workflow_enabled", {})}


@router.post("/api/conversations/{cid}/workflows/{workflow_id}/trigger")
async def api_trigger_workflow(
    cid: str,
    workflow_id: str,
    body: dict = Body(default={}),  # noqa: B008
    job: str | None = None,
):
    """Run a workflow's on_demand hook against the current conversation state.

    The hook runs as a workflow job, so Stop can cancel a long on-demand render
    such as speech; a streaming result is stopped by closing its stream.
    """
    # The body is free-form; only an integer message id names a source a message delete must stop.
    source = body.get("message_id")
    message_id = source if type(source) is int else None
    result = await _finished_job(start_workflow_job(cid, _trigger(cid, workflow_id, body), job=job, message_id=message_id))
    # A streaming result is wrapped by the API layer -- the workflow returns a transport-neutral WorkflowEventStream, never an
    # HTTP response. The response is built after the workflow locks release: the event iterator is lazy, so the hook's DB/prefix
    # prep ran under the locks while the stream itself runs lock-free (matching the pre-refactor behavior). A dict is a plain
    # JSON body.
    if isinstance(result, WorkflowEventStream):
        return workflow_event_stream_response(result, cid=cid, job=job, message_id=message_id)
    return result


async def _trigger(cid: str, workflow_id: str, body: dict) -> Any:
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    # Gate before the lock so a disabled-workflow request does no DB work.
    settings_snapshot = await get_settings()
    sub = _gate_workflow_sub(
        get_subscription(workflow_id, HookType.ON_DEMAND),
        workflow_id,
        settings_snapshot,
        action="on-demand trigger",
        detail=f"Workflow {workflow_id!r} has no on_demand handler",
    )
    # Serialize against the pre/post hook iteration of an in-flight pipeline and against any other /trigger for the same (cid,
    # workflow_id), so the prior workflow_state read the hook depends on cannot be clobbered between read and write by a
    # concurrent caller.
    async with workflow_state_lock(cid, workflow_id):
        conv = await get_conversation(cid)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        msgs = await get_messages(cid)
        explicit_message = None
        if type(body.get("message_id")) is int:
            candidate = await get_message_by_id(body["message_id"])
            if candidate is not None and candidate.get("conversation_id") == cid:
                explicit_message = candidate
        card_id, card = await _resolve_workflow_character(
            conv,
            msgs,
            target_message=explicit_message,
            speaker_member_id=body.get("speaker_member_id") if isinstance(body.get("speaker_member_id"), str) else None,
        )
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        client = client_from_settings(settings_snapshot)
        agent_client, agent_model_name = agent_lane_from_settings(settings_snapshot, writer_client=client)
        async with workflow_character_state_lock(card_id or "", workflow_id):
            with _hook_failures("on_demand hook", workflow_id, defect="On-demand handler raised; see server logs"):
                od_ctx = OnDemandCtx(
                    conversation_id=cid,
                    history=readonly_view(msgs),
                    last_user_message=last_user,
                    settings=readonly_view(settings_snapshot),
                    client=client,
                    agent_client=agent_client,
                    agent_model_name=agent_model_name,
                    character_id=card_id,
                    character=readonly_view(card),
                )
                return await sub.callable(od_ctx, body)


async def _finished_job(task: asyncio.Task[Any]) -> Any:
    """A workflow job's result; one that Stop cancelled answers 409."""
    await asyncio.wait({task})
    if task.cancelled():
        raise HTTPException(status_code=409, detail="Stopped")
    return task.result()


@router.post("/api/conversations/{cid}/workflows/stop")
async def api_stop_workflow_jobs(cid: str, job: str | None = None):
    """Stop the conversation's workflow renders, or only the one named *job*.

    Answers once they have ended, bounded; ``settled`` is False when one was still winding down at the deadline. An on-demand
    stream is stopped by closing it instead.
    """
    result = await stop_workflow_jobs(cid, job=job)
    if result["stopped"]:
        logger.info("Stopped %d workflow job(s) for conversation %s", result["stopped"], scrub_log(cid))
    return {"ok": True, **result}


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate")
async def api_regenerate_attachment(
    cid: str,
    mid: int,
    aid: int,
    request: Request,
    body: dict = Body(default={}),  # noqa: B008
    conv: ConversationRow = Depends(require_conversation),  # noqa: B008
    job: str | None = None,
):
    """Append a new sibling variant under a workflow-produced attachment's root."""
    updates: asyncio.Queue[tuple[str, dict] | None] = asyncio.Queue()
    # A job, not the request: it outlives a dropped stream, so the sibling still
    # lands for the client's recovery poll, and only Stop cancels it.
    task = start_workflow_job(
        cid,
        _regenerate(
            cid,
            mid,
            aid,
            body,
            conv,
            phase=lambda label: updates.put_nowait(("phase_status", {"label": label})),
            landed=lambda new_id: updates.put_nowait(("regenerate_sibling", {"attachment_id": new_id})),
            emit=lambda event, data: updates.put_nowait((event, data)),
        ),
        job=job,
        message_id=mid,
    )
    if "text/event-stream" not in request.headers.get("accept", ""):
        return await _finished_job(task)
    task.add_done_callback(lambda _: updates.put_nowait(None))

    async def events() -> AsyncIterator[PublicEvent]:
        while (update := await updates.get()) is not None:
            yield {"event": update[0], "data": update[1]}
        try:
            yield {"event": "regenerate_done", "data": await _finished_job(task)}
        except API_PASSTHROUGH_ERRORS as exc:
            error = http_failure(exc)
            logger.warning("Regenerate failed: %s", error.detail)
            yield {"event": "regenerate_error", "data": {"status": error.status_code, "detail": error.detail}}

    return workflow_event_stream_response(WorkflowEventStream(events=events()))


_REGENERATE_EVENTS = frozenset({"phase_status", "regenerate_sibling", "regenerate_done", "regenerate_error"})


def _regenerate_emitter(wid: str, send: Callable[[str, dict], None]) -> Callable[[str, dict], None]:
    """`ctx.emit` for one workflow's regenerate: its own prefixed events pass, and
    anything else is dropped with one log line. Dropped rather than raised, because a
    malformed status event must not abort the render it describes."""
    logged = False

    def emit(event: str, data: dict) -> None:
        nonlocal logged
        name_ok = isinstance(event, str) and event.startswith(f"{wid}_") and event not in _REGENERATE_EVENTS
        error = (
            public_event_error({"event": event, "data": data})
            if name_ok
            else "event name must start with the workflow id and not be reserved"
        )
        if error is None:
            send(event, data)
        elif not logged:
            logged = True
            logger.warning("regenerate hook %r emitted %r, dropped: %s", scrub_log(wid), scrub_log(event), error)

    return emit


def _shape_rejection(candidate: Mapping[str, Any], reason: str | None, workflow_id: str, root_id: int) -> dict:
    return {
        "filename": candidate.get("filename") if isinstance(candidate.get("filename"), str) else None,
        "workflow_id": workflow_id,
        "mime": candidate.get("mime") if isinstance(candidate.get("mime"), str) else None,
        "reason": reason,
        "originating_attachment_id": root_id,
    }


async def _regenerate(
    cid: str,
    mid: int,
    aid: int,
    body: dict,
    conv: ConversationRow,
    *,
    phase: Callable[[str], None],
    landed: Callable[[int], None],
    emit: Callable[[str, dict], None],
) -> dict:
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    wid = att.get("workflow_id")
    settings_snapshot = await get_settings()
    sub = _gate_workflow_sub(
        get_subscription(wid, HookType.REGENERATE) if wid else None,
        wid,
        settings_snapshot,
        action="regenerate",
        detail=f"Workflow {wid!r} is not registered or has no regenerate handler",
    )
    # The group root is a mutable identity (deleting a root promotes a sibling to root), so root resolution and locking happen
    # together under locked_attachment_group: it re-reads `att` under the canonical-root lock and retries if a concurrent delete
    # moved the root, so the hook never runs against a since-deleted parent. The dispatcher assigns parent_attachment_id =
    # root_id on every write, so the variant tree stays flat (root + N siblings).
    async with locked_attachment_group(aid, mid) as (att, root_id):
        anchor = await get_message_by_id(mid)
        if anchor is None or anchor["conversation_id"] != cid:
            raise HTTPException(status_code=404, detail="Message not found in conversation")
        msgs = await get_messages_before(cid, mid)
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        client = client_from_settings(settings_snapshot)
        agent_client, agent_model_name = agent_lane_from_settings(settings_snapshot, writer_client=client)

        card_id, card = await _resolve_workflow_character(conv, list(msgs), target_message=anchor)
        initially_shown = await variant_on_show(root_id)
        kept: list[int] = []
        rejections: list[dict] = []

        def candidate_of(attachment: dict, **extra: Any) -> dict:
            # Stamped with the text it renders: an edit made meanwhile keeps it from auto-activating.
            metadata = {**(attachment.get("generation_metadata") or {}), "source_text": anchor["content"]}
            return {**attachment, "workflow_id": sub.workflow_id, **extra, "generation_metadata": metadata}

        async def keep(attachment: dict) -> int | None:
            candidate = candidate_of(attachment)
            ok, reason = validate_workflow_attachment_shape(candidate)
            if not ok:
                rejections.append(_shape_rejection(candidate, reason, sub.workflow_id, root_id))
                return None
            # The group lock is held, so the root cannot move; `shown` leaves a user
            # who paged away from the previous render where they are.
            new_id, rejected = await insert_workflow_variant(
                mid, candidate, group=[root_id], shown=kept[-1] if kept else initially_shown
            )
            if rejected is not None:
                rejections.append(project_rejected_attachment(rejected, root_id))
            if new_id is None:
                return None
            kept.append(new_id)
            landed(new_id)
            return new_id

        with _hook_failures("regenerate hook", wid, aid, defect="Regenerate handler raised; see server logs"):
            regen_ctx = RegenCtx(
                conversation_id=cid,
                message_id=mid,
                attachment_id=aid,
                original_attachment=readonly_view(att),
                history=readonly_view(msgs),
                last_user_message=last_user,
                settings=readonly_view(settings_snapshot),
                client=client,
                agent_client=agent_client,
                agent_model_name=agent_model_name,
                character_id=card_id,
                character=readonly_view(card),
                phase=phase,
                keep=keep,
                emit=_regenerate_emitter(sub.workflow_id, emit),
            )
            new_dicts = await sub.callable(regen_ctx, body)

        if not isinstance(new_dicts, list):
            logger.warning("regenerate hook %r returned non-list (%s); treating as empty", wid, type(new_dicts).__name__)
            new_dicts = []

        # Bad-shape entries are partitioned to rejected_workflow_atts so a single bad entry does not roll back the batch insert.
        # Non-dict entries are dropped instead of rejected because the rejection record requires a filename to surface in the
        # UI.
        fixed: list[dict] = []
        for d in new_dicts:
            if not isinstance(d, dict):
                logger.warning("regenerate hook %r returned non-dict entry; skipping", wid)
                continue
            candidate = candidate_of(d, parent_attachment_id=root_id)
            ok, reason = validate_workflow_attachment_shape(candidate)
            if not ok:
                rejections.append(_shape_rejection(candidate, reason, sub.workflow_id, root_id))
                logger.info("regenerate hook %r returned attachment rejected by shape validator: %s", wid, reason)
                continue
            fixed.append(candidate)

        if not fixed:
            return {"attachments": kept, "rejected_workflow_atts": rejections}

        try:
            with committing_workflow_job():
                new_ids, helper_rejected = await insert_workflow_attachments(
                    mid, fixed, shown=kept[-1] if kept else initially_shown
                )
        except (ValueError, LookupError, OSError):
            logger.exception("regenerate hook %r batch insert failed", wid)
            raise HTTPException(status_code=500, detail="Regenerate batch insert failed; see server logs") from None

        helper_rejected_projected = [project_rejected_attachment(a, root_id) for a in helper_rejected]
        return {"attachments": kept + new_ids, "rejected_workflow_atts": rejections + helper_rejected_projected}


def _decode_stored_consumption_metadata(att: Mapping[str, Any]) -> dict | None:
    """Parse the parent attachment's stored consumption_metadata JSON.

    Returns the decoded dict, or ``None`` for any malformed or non-dict value.
    """
    raw = att.get("consumption_metadata")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _decode_generation_params(att: Mapping[str, Any]) -> dict:
    """Decode an attachment's stored generation_metadata into a params dict.

    Malformed or non-dict values coerce to ``{}`` so a reroll/rehydrate
    proceeds with defaults instead of failing on bad stored metadata.
    """
    raw = att.get("generation_metadata")
    try:
        params = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return params if isinstance(params, dict) else {}


def _apply_param_overrides(params: dict, body: Mapping[str, Any] | None) -> None:
    """Merge request overrides into stored generation parameters."""
    overrides = body.get("params") if isinstance(body, Mapping) else None
    if not isinstance(overrides, Mapping):
        return
    for key, value in overrides.items():
        if isinstance(value, str) and isinstance(params.get(key), str):
            params[key] = value


def _split_reroll_gen_result(result, workflow_id: str | None) -> tuple[object, dict | None]:
    """Normalize reroll_gen results to ``(data, consumption_metadata)``.

    Bytes have no metadata; tuples may supply a dict. Warn and drop other metadata shapes. The caller validates non-empty bytes.
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (bytes, bytearray)):
        data, consumption_metadata = result
        if consumption_metadata is not None and not isinstance(consumption_metadata, dict):
            logger.warning(
                "reroll_gen hook %r returned tuple with non-dict consumption_metadata (%s); coercing to None",
                workflow_id,
                type(consumption_metadata).__name__,
            )
            consumption_metadata = None
        return data, consumption_metadata
    return result, None


def _build_reroll_gen_ctx(
    cid: str, mid: int, aid: int, att: Mapping[str, Any], settings: Mapping[str, Any], client, *, replay: bool
) -> RerollGenCtx:
    prior_cm = _decode_stored_consumption_metadata(att)
    return RerollGenCtx(
        conversation_id=cid,
        message_id=mid,
        attachment_id=aid,
        original_attachment=readonly_view(att),
        settings=readonly_view(settings),
        client=client,
        prior_consumption_metadata=readonly_view(prior_cm) if prior_cm is not None else None,
        # Keyword-only and required, so the two routes cannot share this builder
        # while silently sharing an answer they disagree about.
        replay=replay,
    )


def _generated_seed() -> str:
    return secrets.token_hex(16)


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen")
async def api_reroll_gen_attachment(
    cid: str,
    mid: int,
    aid: int,
    body: dict = Body(default={}),  # noqa: B008
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
    job: str | None = None,
):
    """Render a new sibling with a fresh seed and inherited generation metadata.

    Persist the new seed for rehydration. Optional params overrides are passed
    to the hook; store its amended params for future rerolls.
    """
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    wid = att.get("workflow_id")
    settings_snapshot = await get_settings()
    sub = _gate_workflow_sub(
        get_subscription(wid, HookType.REROLL_GEN) if wid else None,
        wid,
        settings_snapshot,
        action="reroll-gen",
        detail=f"Workflow {wid!r} is not registered or has no reroll_gen handler",
    )

    return await _finished_job(
        start_workflow_job(cid, _reroll_gen(cid, mid, aid, body, sub, settings_snapshot), job=job, message_id=mid)
    )


async def _reroll_gen(
    cid: str, mid: int, aid: int, body: dict, sub: Subscription[RerollGenHook], settings_snapshot: Mapping[str, Any]
) -> dict:
    wid = sub.workflow_id
    # Resolve-and-lock the canonical root together (see regenerate): the in-lock
    # snapshot and root id are read under the same lock the write will hold.
    async with locked_attachment_group(aid, mid) as (att, root_id):
        initially_shown = await variant_on_show(root_id)
        params = _decode_generation_params(att)
        anchor = await get_message_by_id(mid)
        if anchor is None or anchor["conversation_id"] != cid:
            raise HTTPException(status_code=404, detail="Message not found in conversation")
        source_text = params.pop("source_text", anchor["content"])
        _apply_param_overrides(params, body)
        seed = _generated_seed()
        client = client_from_settings(settings_snapshot)

        with _hook_failures("reroll_gen hook", wid, aid, defect="reroll_gen handler raised; see server logs"):
            # Not a replay: this route promises another variant of the same subject, not the stored image back, so a workflow
            # whose configuration has moved renders on today's.
            ctx = _build_reroll_gen_ctx(cid, mid, aid, att, settings_snapshot, client, replay=False)
            result = await sub.callable(ctx, params, seed)

        data, new_consumption_metadata = _split_reroll_gen_result(result, wid)

        if not isinstance(data, (bytes, bytearray)) or not data:
            raise HTTPException(status_code=500, detail="reroll_gen handler returned no bytes")

        new_attachment = {
            "workflow_id": sub.workflow_id,
            "parent_attachment_id": root_id,
            "filename": att.get("filename") or sub.workflow_id,
            "mime": att.get("mime_type") or "application/octet-stream",
            "data": bytes(data),
            "seed": seed,
            "generation_metadata": {**params, "source_text": source_text},
            "consumption_metadata": new_consumption_metadata,
            "annotation": att.get("annotation"),
        }
        try:
            with committing_workflow_job():
                new_id, rejected = await insert_workflow_attachment(mid, new_attachment, shown=initially_shown)
        except (ValueError, LookupError, OSError):
            logger.exception("reroll_gen hook %r yielded an attachment that failed insert", wid)
            raise HTTPException(status_code=500, detail="reroll_gen insert failed; see server logs") from None

        return {
            "attachment_id": new_id,
            "rejected_workflow_atts": ([project_rejected_attachment(rejected, root_id)] if rejected is not None else []),
        }


@router.get("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/in-flight")
async def api_workflow_attachment_in_flight(
    cid: str,
    mid: int,
    aid: int,
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Report whether an operation holds this attachment group's lock.

    Resolve any member id to its canonical root so dropped requests can be polled.
    """
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    root_id = att["parent_attachment_id"] or att["id"]
    return {"in_flight": workflow_group_in_flight(root_id)}


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate")
async def api_rehydrate_attachment(
    cid: str,
    mid: int,
    aid: int,
    body: dict = Body(default={}),  # noqa: B008, ARG001
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
    job: str | None = None,
):
    """Rehydrate an EVICTED_MARKER row using its non-NULL seed and stored params.

    Write reroll_gen bytes back to the same row without creating a sibling.
    """
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    if att.get("data_b64") != EVICTED_MARKER:
        raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate")
    seed = att.get("seed")
    if not seed:
        raise HTTPException(status_code=409, detail="Attachment has no stored seed; cannot rehydrate")

    # Gate before the root lock: rehydrate re-synthesizes evicted bytes by running the workflow's generative REROLL_GEN hook (an
    # LLM call for tts) -- the same hook reroll-gen gates -- so a disabled workflow must not fire it. An artifact evicted while
    # off therefore needs a re-enable to restore (no data loss; the row and seed persist). workflow_id is stable across the
    # in-lock re-read, so the pre-lock att is a safe source for the gate.
    wid = att.get("workflow_id")
    settings_snapshot = await get_settings()
    _gate_workflow_sub(
        get_subscription(wid, HookType.REROLL_GEN) if wid else None,
        wid,
        settings_snapshot,
        action="rehydrate",
        detail=f"Workflow {wid!r} is not registered or has no reroll_gen handler",
    )
    return await _finished_job(
        start_workflow_job(cid, _rehydrate(cid, mid, aid, seed, settings_snapshot), job=job, message_id=mid)
    )


async def _rehydrate(cid: str, mid: int, aid: int, seed: str, settings_snapshot: Mapping[str, Any]) -> dict:
    # Serialize same-root rehydrates the way /regenerate and /reroll-gen already do for their sibling-tree mutations. Without
    # this, two concurrent callers would each run the full reroll_gen LLM call before the cache helper's transactional recheck
    # deduplicates them at the DB layer -- doubling LLM cost even though the row stays consistent. locked_attachment_group holds
    # the canonical-root lock and re-reads `att` under it.
    async with locked_attachment_group(aid, mid) as (att, _root_id):
        # Re-check the eviction precondition on the in-lock snapshot so a concurrent caller that already rehydrated cannot slip
        # past the pre-lock check and double the reroll_gen LLM call before the cache helper's transactional recheck
        # deduplicates the bytes write.
        if att.get("data_b64") != EVICTED_MARKER:
            raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate")
        wid = att.get("workflow_id")
        sub = get_subscription(wid, HookType.REROLL_GEN) if wid else None
        if sub is None:
            raise HTTPException(status_code=404, detail=f"Workflow {wid!r} is not registered or has no reroll_gen handler")

        params = _decode_generation_params(att)

        client = client_from_settings(settings_snapshot)

        with _hook_failures("reroll_gen (rehydrate)", wid, aid, defect="reroll_gen handler raised; see server logs"):
            # A replay: these bytes are meant to be the ones this row lost, so every
            # stored parameter is reproduced rather than re-read from settings.
            ctx = _build_reroll_gen_ctx(cid, mid, aid, att, settings_snapshot, client, replay=True)
            result = await sub.callable(ctx, params, seed)

        data, new_consumption_metadata = _split_reroll_gen_result(result, wid)

        if not isinstance(data, (bytes, bytearray)) or not data:
            raise HTTPException(status_code=500, detail="reroll_gen handler returned no bytes")

        try:
            with committing_workflow_job():
                await rehydrate_attachment(aid, bytes(data), consumption_metadata=new_consumption_metadata)
        except RehydrateAlreadyDoneError:
            # Race with a concurrent rehydrate that already restored the bytes. End state is correct; surface as 409 so the
            # client treats it as success rather than the generic 500.
            raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate") from None
        except (LookupError, ValueError):
            logger.exception("rehydrate write failed for attachment %r", scrub_log(aid))
            raise HTTPException(status_code=500, detail="rehydrate write failed; see server logs") from None

        return {"attachment_id": aid}


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/activate")
async def api_activate_workflow_attachment(
    cid: str,
    mid: int,
    aid: int,
    body: dict = Body(default={}),  # noqa: B008
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Persist the user's active-sibling choice for a workflow attachment group.

    ``aid`` is the ROOT attachment id (``parent_attachment_id IS NULL``). Body shape: ``{"sibling_id": int | null}`` -- ``null``
    clears the column, which reverts to "newest sibling wins" in the renderer.
    """
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")

    raw_sibling_id = body.get("sibling_id") if isinstance(body, dict) else None
    if raw_sibling_id is not None and (not isinstance(raw_sibling_id, int) or isinstance(raw_sibling_id, bool)):
        raise HTTPException(status_code=400, detail="sibling_id must be an integer or null")

    # Keep swipes outside the render lock so navigation stays responsive. set_active_sibling validates membership in BEGIN
    # IMMEDIATE; concurrent swipes and renders use last-commit ordering for the active pointer.
    try:
        await set_active_sibling(aid, raw_sibling_id, expected_message_id=mid)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"active_sibling_id": raw_sibling_id}


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete")
async def api_delete_workflow_attachment(
    cid: str,
    mid: int,
    aid: int,
    body: dict = Body(default={}),  # noqa: B008
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Delete a workflow attachment: one variant, or the whole group.

    ``aid`` is the acted-on row. Body: ``{"scope": "variant" | "group"}``. Deleting the root variant of a multi-variant group
    promotes the oldest survivor to root; the response ``root_id`` reports the resulting root.
    """
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    scope = body.get("scope") if isinstance(body, dict) else None
    if scope not in ("variant", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'variant' or 'group'")
    # locked_attachment_group resolves the canonical root and locks it, retrying if a concurrent delete promotes the root
    # mid-acquire -- so a delete that promotes a sibling and a delete racing it never end up holding different keys for what is
    # now one group. delete_workflow_attachments re-derives the root in its own BEGIN IMMEDIATE, which remains the integrity
    # boundary.
    try:
        async with locked_attachment_group(aid, mid) as (_att, _root_id):
            result = await delete_workflow_attachments(aid, scope=scope, expected_message_id=mid)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.get("/api/workflow-attachments/{aid}/content")
async def api_get_workflow_attachment_content(aid: int, request: Request):
    """The attachment's bytes, which the message listing leaves out.

    Not gated on the workflow being enabled: stored artifacts stay readable.
    """
    att = await get_workflow_attachment_by_id(aid)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    if att["data_b64"] == EVICTED_MARKER:
        raise HTTPException(status_code=410, detail="Attachment bytes were evicted")
    return attachment_content_response(att["data_b64"], att["mime_type"], request)


def _download_name(name: str) -> str:
    """A filename safe to quote into Content-Disposition."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._") or "attachment"


@router.get("/api/workflow-attachments/{aid}/export")
async def api_export_workflow_attachment(aid: int):
    """Download the stored artifact through its workflow's export hook.

    Enabled state is irrelevant; load bytes only if the hook needs them. X-Orb-Export-Note carries any export note.
    """
    att = await get_workflow_attachment_meta(aid)
    if att is None:
        raise HTTPException(status_code=404, detail="Attachment not found")
    wid = att["workflow_id"]
    sub = get_subscription(wid, HookType.EXPORT)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"Workflow {wid!r} does not export attachments")
    consumption = _decode_stored_consumption_metadata(att)
    ctx = ExportCtx(
        attachment_id=aid,
        attachment=readonly_view(att),
        consumption_metadata=readonly_view(consumption) if consumption is not None else None,
        stored_bytes=partial(get_workflow_attachment_bytes, aid),
    )
    with _hook_failures("export hook", wid, aid, defect="Export handler raised; see server logs"):
        exported = await sub.callable(ctx)
    if exported is None:
        raise HTTPException(status_code=410, detail="Attachment bytes were evicted")
    if not isinstance(exported, ExportedFile) or not exported.data:
        logger.error("export hook %r returned no file for attachment %r", scrub_log(wid), scrub_log(aid))
        raise HTTPException(status_code=500, detail="Export handler returned no file")
    headers = {
        "Content-Disposition": f'attachment; filename="{_download_name(exported.filename)}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    note = " ".join(exported.note.split()).encode("ascii", "replace").decode("ascii")
    if note:
        headers["X-Orb-Export-Note"] = note
    return Response(content=bytes(exported.data), media_type=exported.mime, headers=headers)


@router.post("/api/conversations/{cid}/workflow-attachments/access")
async def api_record_workflow_attachment_access(
    cid: str,
    body: dict = Body(default={}),  # noqa: B008
    _conv: ConversationRow = Depends(require_conversation),  # noqa: B008
):
    """Record attachment access for ``{"ids": [int, ...]}`` in input order.

    Silently skip foreign or stale ids to tolerate swipe/regeneration races.
    """
    raw_ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list of integers")

    int_ids: list[int] = []
    for v in raw_ids:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            int_ids.append(v)

    if not int_ids:
        return {"ok": True, "recorded": 0}

    valid_ids = await conversation_attachment_ids(cid, int_ids)
    ordered_valid = [i for i in int_ids if i in valid_ids]

    await record_access(ordered_valid)
    return {"ok": True, "recorded": len(ordered_valid)}
