"""The assistant's media jobs: one saved reply rendered by its workflow's own on-demand hook.

A job runs the hook through the shared dispatch the trigger route uses, as one workflow job filed under its run (so the
assistant's Stop cancels that job alone) and under the message it renders (so deleting the message or its chat finds and
settles it). A lazy event stream the hook returns is consumed inside that same job until it settles. A
result names the attachments that job reported saving; a job that started and saved nothing is not a success.

The hooks are reached through the contract the frontend uses over HTTP: an ``action`` and a ``message_id`` in the body (and for
an image, an optional ``direction``), then
either a JSON result with ``attachment_id`` or ``error``, or a stream of ``<workflow>_render`` / ``<workflow>_error`` /
``<workflow>_done`` events beside ``phase_status`` labels. Nothing here imports a plug-in.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Mapping
from typing import Any

from fastapi import HTTPException

from ...features.assistant.tools import HostRefusal, MediaOutcome, MediaRun
from ...inference import AbortToken
from ...workflows import HookType, WorkflowEventStream, get_subscription, get_workflow, public_event_error
from ..deps import start_workflow_job, stop_workflow_jobs
from ..errors import API_PASSTHROUGH_ERRORS, http_failure
from ..workflow_dispatch import run_on_demand

logger = logging.getLogger(__name__)

_LABEL_CHARS = 160
_ERROR_CHARS = 300


def media_available(workflow_id: str) -> bool:
    return get_workflow(workflow_id) is not None and bool(get_subscription(workflow_id, HookType.ON_DEMAND))


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


async def _consume(
    stream: WorkflowEventStream, wid: str, noun: str, outcome: MediaOutcome, report: Callable[[str], None]
) -> None:
    """Read a lazy workflow stream through to its end, inside the job that owns it.

    A cancelled job cancels the stream's pending step, so the hook's own cleanup (withdrawing a queued render) runs there.
    """
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
                outcome.error = _message_of(payload, f"The {noun} failed.")
            elif name == f"{wid}_done":
                outcome.saved(payload.get("attachment_id"))
                outcome.finished = True
            elif name == "error":
                outcome.error = _message_of(data, f"The {noun} failed.")
    finally:
        close = getattr(events, "aclose", None)
        if close is not None:
            with contextlib.suppress(Exception):
                await close()


async def _produce(
    cid: str, wid: str, noun: str, body: Mapping[str, Any], outcome: MediaOutcome, report: Callable[[str], None]
) -> None:
    """The job: the hook through the shared dispatch with *body*, and its stream if it returns one."""
    mid = body["message_id"]
    try:
        result = await run_on_demand(cid, wid, dict(body))
        if isinstance(result, WorkflowEventStream):
            await _consume(result, wid, noun, outcome, report)
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
        logger.exception("Assistant %s for message %s failed", noun, mid)
        outcome.error = f"The {noun} failed; the server log has the details."


async def _waited(task: asyncio.Task[Any], abort: AbortToken) -> bool:
    """Wait for *task* or the run's Stop, whichever is first; ``True`` when Stop came first."""
    stop = asyncio.ensure_future(abort.wait())
    try:
        await asyncio.wait({task, stop}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop.cancel()
    return not task.done()


async def run_media(cid: str, workflow_id: str, noun: str, body: Mapping[str, Any], run: MediaRun) -> MediaOutcome:
    """One job of *run* for *body*'s message, waited for until it ends or the run's Stop; what it reported."""
    outcome = MediaOutcome()
    try:
        task = start_workflow_job(
            cid, _produce(cid, workflow_id, noun, body, outcome, run.report), job=run.job, message_id=body["message_id"]
        )
    except HTTPException as exc:
        raise HostRefusal(str(exc.detail)) from None
    outcome.stopped = await _waited(task, run.abort)
    if outcome.stopped:
        # This run's job alone: a render the user started from the message, or another run's, is left running.
        if not (await stop_workflow_jobs(cid, job=run.job))["settled"]:
            # Its result is not known until it has ended, and the browser reads the message again only once this run has.
            await asyncio.wait({task})
    outcome.cut = task.cancelled()
    return outcome
