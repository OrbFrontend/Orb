"""The assistant's media tools: an image or speech for one saved reply, made by its workflow's own on-demand hook.

A tool runs the hook through the shared dispatch the trigger route uses, minus the route's enablement gate, as one workflow job
filed under its run (so the assistant's Stop cancels that job alone) and under the message it renders (so deleting the message
or its chat finds and settles it). A lazy event stream the hook returns is consumed inside that same job until it settles. A
result names the attachments that job reported saving; a job that started and saved nothing is not a success.

The hooks are reached through the contract the frontend uses over HTTP: an ``action`` and a ``message_id`` in the body, then
either a JSON result with ``attachment_id`` or ``error``, or a stream of ``<workflow>_render`` / ``<workflow>_error`` /
``<workflow>_done`` events beside ``phase_status`` labels. Nothing here imports a plug-in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException
from pydantic import Field

from ..database import get_message_by_id, get_settings, message_workflow_attachment_ids
from ..features.assistant import AssistantTool, ToolArguments, ToolFailure, ToolOutput, ViewContext
from ..inference import AbortToken
from ..workflows import (
    HookType,
    WorkflowEventStream,
    get_subscription,
    get_workflow,
    gpu_swap_host,
    public_event_error,
)
from ..workflows.enablement import effective_workflow_enabled
from .assistant_edits import ID_CHARS, quoted, reply_target
from .deps import message_deleting, resource_deleting, start_workflow_job, stop_workflow_jobs
from .errors import API_PASSTHROUGH_ERRORS, http_failure
from .workflow_dispatch import run_on_demand

logger = logging.getLogger(__name__)

_LABEL_CHARS = 160
_ERROR_CHARS = 300


@dataclass(frozen=True)
class MediaRun:
    """What the media tools take from the run they belong to.

    *job* is the workflow job id their jobs are filed under, unique to the run's operation; *abort* is the run's Stop; *report*
    sends a progress label to the run's stream while a job renders.
    """

    job: str
    abort: AbortToken
    report: Callable[[str], None]


@dataclass(frozen=True)
class _Medium:
    tool: str
    workflow_id: str
    action: str
    noun: str
    # The noun with its article, as a sentence names one: "an image", "speech".
    some: str
    # Completes "only a reply can be …".
    verb: str
    starting: str
    description: str
    # What the result adds while the workflow is switched off: the saved artifact shows either way, but remaking it needs it on.
    while_off: str = ""


_MEDIA = (
    _Medium(
        "generate_image",
        "image_gen",
        "generate",
        "image",
        "an image",
        "illustrated",
        "Composing the image prompt…",
        "Make an image of one saved reply in a chat with the Image Generation workflow, as the reply's image button does, and "
        "save it on that message. It takes a while, often minutes.",
    ),
    _Medium(
        "generate_speech",
        "tts",
        "create",
        "speech",
        "speech",
        "voiced",
        "Synthesizing speech…",
        "Make speech audio of one saved reply in a chat with the Text-to-Speech workflow, in the speaking character's voice, "
        "and save it on that message.",
        while_off="Text-to-Speech is switched off: the message's player plays this clip, and remaking it needs the workflow on.",
    ),
)

_DISABLED_GPU = (
    "Image Generation is switched off, and its ComfyUI setup asks to swap the GPU with the chat model, which happens only while "
    "the workflow is on. Rendering now would load ComfyUI beside the chat model, so nothing was started and no setting was "
    "changed. Tell the user; they can turn Image Generation on (or ask you to), or turn off GPU swap in its setup."
)


class _MediaArgs(ToolArguments):
    conversation_id: str = Field(default="", max_length=ID_CHARS, description="The chat's id; empty means the chat in view.")
    message_id: int = Field(ge=1, description="The saved reply to make it of, from read_messages.")


@dataclass
class _Outcome:
    """What one job reported: the attachments it saved, its error, and whether its hook reached its own end."""

    ids: list[int] = field(default_factory=list)
    error: str = ""
    finished: bool = False

    def saved(self, attachment: object) -> None:
        if type(attachment) is int and attachment not in self.ids:
            self.ids.append(attachment)


def _detail(exc: Exception) -> str:
    detail = http_failure(exc).detail
    if isinstance(detail, Mapping):
        detail = detail.get("message") or detail.get("detail") or ""
    return str(detail or "The workflow refused the request.")[:_ERROR_CHARS]


def _message_of(data: object, fallback: str) -> str:
    if isinstance(data, Mapping):
        for key in ("message", "sentence", "headline"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:_ERROR_CHARS]
    if isinstance(data, str) and data.strip():
        return data.strip()[:_ERROR_CHARS]
    return fallback


async def _consume(stream: WorkflowEventStream, medium: _Medium, outcome: _Outcome, report: Callable[[str], None]) -> None:
    """Read a lazy workflow stream through to its end, inside the job that owns it.

    A cancelled job cancels the stream's pending step, so the hook's own cleanup (withdrawing a queued render) runs there.
    """
    wid = medium.workflow_id
    events = stream.events
    try:
        async for event in events:
            if public_event_error(event) is not None:
                continue
            name, data = event["event"], event.get("data")
            payload = data if isinstance(data, Mapping) else {}
            if name == "phase_status":
                label = payload.get("label")
                if isinstance(label, str) and label.strip():
                    report(label.strip()[:_LABEL_CHARS])
            elif name == f"{wid}_render":
                outcome.saved(payload.get("attachment_id"))
            elif name == f"{wid}_error":
                outcome.error = _message_of(payload, f"The {medium.noun} failed.")
            elif name == f"{wid}_done":
                outcome.saved(payload.get("attachment_id"))
                outcome.finished = True
            elif name == "error":
                outcome.error = _message_of(data, f"The {medium.noun} failed.")
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


async def _produce(cid: str, medium: _Medium, mid: int, outcome: _Outcome, report: Callable[[str], None]) -> None:
    """The job: the hook through the shared dispatch, without the route's enablement gate, and its stream if it returns one."""
    try:
        result = await run_on_demand(
            cid, medium.workflow_id, {"action": medium.action, "message_id": mid}, require_enabled=False
        )
        if isinstance(result, WorkflowEventStream):
            await _consume(result, medium, outcome, report)
            return
        outcome.finished = True
        if not isinstance(result, Mapping):
            outcome.error = "The workflow returned no result."
            return
        outcome.saved(result.get("attachment_id"))
        error, reason = result.get("error"), result.get("reason")
        if isinstance(error, str) and error:
            outcome.error = (f"{error} ({reason})" if isinstance(reason, str) and reason else error)[:_ERROR_CHARS]
    except API_PASSTHROUGH_ERRORS as exc:
        outcome.error = _detail(exc)
    except Exception:
        logger.exception("Assistant %s for message %s failed", medium.noun, mid)
        outcome.error = f"The {medium.noun} failed; the server log has the details."


def build_media_tools(view: ViewContext, run: MediaRun | None) -> list[AssistantTool]:
    """One tool per media workflow that is installed with an on-demand hook; none without a run to report to."""
    if run is None:
        return []

    def tool(medium: _Medium) -> AssistantTool:
        async def make(args: _MediaArgs) -> ToolOutput:
            return await _make(view, run, medium, args)

        return AssistantTool(
            medium.tool,
            medium.description,
            _MediaArgs,
            "write",
            make,
            target=lambda args: {
                "kind": "media",
                "conversation_id": args.conversation_id or view.conversation_id,
                "message_id": args.message_id,
            },
            reports_progress=True,
        )

    return [
        tool(medium)
        for medium in _MEDIA
        if get_workflow(medium.workflow_id) is not None and get_subscription(medium.workflow_id, HookType.ON_DEMAND)
    ]


async def _waited(task: asyncio.Task[Any], abort: AbortToken) -> bool:
    """Wait for *task* or the run's Stop, whichever is first; ``True`` when Stop came first."""
    stop = asyncio.ensure_future(abort.wait())
    try:
        await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
    return not task.done()


async def _make(view: ViewContext, run: MediaRun, medium: _Medium, args: _MediaArgs) -> ToolOutput:
    conv, message = await reply_target(args.conversation_id, args.message_id, view, medium.verb, idle=False)
    cid, mid = conv["id"], message["id"]
    if not (message["content"] or "").strip():
        raise ToolFailure(f"Message {mid} has no text to make {medium.noun} of.")
    workflow = get_workflow(medium.workflow_id)
    if workflow is None or get_subscription(medium.workflow_id, HookType.ON_DEMAND) is None:
        raise ToolFailure(f"The {medium.noun} workflow is not installed, so nothing was made.")
    # Read now: a toggle earlier in this request may have changed what the request's own snapshot says.
    settings = await get_settings()
    if medium.workflow_id == gpu_swap_host.IMAGE_GEN and await gpu_swap_host.swap_skipped_while_disabled(settings):
        raise ToolFailure(_DISABLED_GPU)
    off = medium.while_off if not effective_workflow_enabled(medium.workflow_id, settings) else ""

    before = set(await message_workflow_attachment_ids(mid, medium.workflow_id))
    outcome = _Outcome()
    run.report(medium.starting)
    try:
        task = start_workflow_job(cid, _produce(cid, medium, mid, outcome, run.report), job=run.job, message_id=mid)
    except HTTPException:
        raise ToolFailure("That chat or message is being deleted, so nothing was made.") from None
    stopped = await _waited(task, run.abort)
    if stopped:
        # This run's job alone: a render the user started from the message, or another run's, is left running.
        if not (await stop_workflow_jobs(cid, job=run.job))["settled"]:
            # Its result is not known until it has ended, and the browser reads the message again only once this run has.
            await asyncio.wait({task})
    return await _result((cid, conv["title"], mid), medium, before, outcome, stopped=stopped, cut=task.cancelled(), off=off)


async def _result(
    where: tuple[str, str, int], medium: _Medium, before: set[int], outcome: _Outcome, *, stopped: bool, cut: bool, off: str
) -> ToolOutput:
    """The tool's answer from what its own job reported: an attachment it saved is success, and a started job alone is not.

    *stopped* is this run's Stop and *cut* a job cancelled by someone else (a deletion). Only the ids the job reported count:
    another render of the same message, such as one the user started from its button, saves rows too.
    """
    cid, title, mid = where
    # A delete settles the job before it deletes, so the message may still be there, about to go.
    if await get_message_by_id(mid) is None or message_deleting(mid) or resource_deleting(cid):
        raise ToolFailure(f"Message {mid} was deleted while its {medium.noun} was being made, so nothing was kept.")
    stored = set(await message_workflow_attachment_ids(mid, medium.workflow_id))
    ids = [attachment for attachment in outcome.ids if attachment in stored]
    if not ids:
        if (stopped or cut or not outcome.finished) and stored - before:
            # A job cut short may have saved a render whose event was never read, but the new row may as well be another's.
            ended = "was stopped" if stopped else "was cut short" if cut else "ended before finishing"
            raise ToolFailure(
                f"The {medium.noun} job {ended} without reporting a saved {medium.noun}, but {medium.some} may have been saved "
                f"on message {mid} as it ended. Tell the user to check the message."
            )
        if stopped:
            raise ToolFailure(f"Stopped before any {medium.noun} was saved; nothing was made.")
        if cut:
            raise ToolFailure(f"The {medium.noun} job was stopped before anything was saved, so nothing was made.")
        raise ToolFailure(f"No {medium.noun} was made: {outcome.error or 'the workflow ended without saving anything.'}")
    what = f"{len(ids)} images" if medium.noun == "image" and len(ids) > 1 else medium.some
    summary = f"Saved {what} on message {mid} in {quoted(title)}"
    if stopped or cut:
        status, summary = "stopped early; kept what was saved", f"{summary} before it was stopped."
    elif outcome.error:
        status = f"saved; a later step failed: {outcome.error}"
        summary = f"{summary}, but a later step failed: {outcome.error}"
    else:
        status, summary = "saved", f"{summary}."
    data: dict[str, Any] = {
        "saved": True,
        "kind": medium.noun,
        "conversation_id": cid,
        "message_id": mid,
        "attachment_ids": ids,
        "status": status,
        "note": f"It is on the message in the chat; nothing else changed. {off}".strip(),
    }
    ids_out = {"conversation_id": cid, "message_id": mid, "attachment_ids": ids}
    return ToolOutput(
        data,
        summary,
        ids_out,
        committed=({"kind": "media", **ids_out, "workflow_id": medium.workflow_id},),
    )
