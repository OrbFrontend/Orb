"""Shared API state, dependencies, and request validators."""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from typing import Any, TypeVar, cast

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..analysis.detectors.slop_detector import MAX_PHRASE_REGEX
from ..database import get_conversation, get_lorebook_entry, get_workflow_attachment_by_id, get_world, get_world_changeset
from ..database.models import ConversationRow
from ..features.cards import ProfileDraftUnavailable
from ..inference import AbortToken
from ..pipeline.events import PublicTurnEvent
from ..workflows import PublicEvent, WorkflowEventStream, public_event_error
from .errors import API_PASSTHROUGH_ERRORS, failure_event
from .work_registry import ActiveStream, ChatUnavailable, work_registry

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend")


# Serialize sibling-tree mutations through commit, preserving response ordering. Renders take no group lock: each saved render
# resolves its surviving root in its own transaction, so a delete or swipe never waits for one.
# Locks persist per interacted root until restart; setdefault has no intervening await.
_workflow_root_locks: dict[int, asyncio.Lock] = {}
_rehydrate_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def rehydrate_lock(attachment_id: int):
    """Serialize rehydrates of one row, so a second caller re-reads the row instead of paying for the render again."""
    lock = _rehydrate_locks.setdefault(attachment_id, asyncio.Lock())
    async with lock:
        yield


@asynccontextmanager
async def _workflow_root_lock(root_id: int):
    lock = _workflow_root_locks.setdefault(root_id, asyncio.Lock())
    async with lock:
        yield


def workflow_group_in_flight(root_id: int) -> bool:
    """Whether an operation holds this group's root lock through commit.

    False may also mean queued before lock acquisition; require repeated idle
    samples before treating a dropped request as finished.
    """
    lock = _workflow_root_locks.get(root_id)
    return lock is not None and lock.locked()


def workflow_message_busy(cid: str, message_id: int) -> bool:
    """Whether a workflow render can still save on *message_id*."""
    return work_registry.message_busy(cid, message_id)


def resource_deleting(key: str) -> bool:
    return work_registry.resource_deleting(key)


def message_deleting(message_id: int) -> bool:
    return work_registry.message_deleting(message_id)


@contextmanager
def idle_chats_guard(rows: Sequence[Mapping[str, Any]]):
    """Refuse all busy phases and fence admission through a destructive write."""
    titles = {str(row["id"]): row.get("title") or str(row["id"]) for row in rows}
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(work_registry.deletion_fence(list(titles), require_idle=True))
        except ChatUnavailable as exc:
            raise HTTPException(status_code=409, detail=f"Stop running work in {titles[exc.key]} first") from None
        yield


@asynccontextmanager
async def deleting_message_sources(cid: str, message_ids: set[int]):
    with contextlib.ExitStack() as stack:
        try:
            stack.enter_context(work_registry.message_deletion_fence(cid, message_ids))
        except ChatUnavailable:
            raise HTTPException(status_code=409, detail="Message deletion already in progress") from None
        result = await stop_workflow_jobs(cid, message_ids=message_ids)
        if not result["settled"]:
            raise HTTPException(status_code=409, detail="Media did not settle; nothing was deleted")
        yield


def active_work() -> list[str]:
    return work_registry.active_work()


@asynccontextmanager
async def deleting_resources(keys: Sequence[str]):
    """Close admission, settle work, then take lanes for the final delete."""
    with contextlib.ExitStack() as fence:
        try:
            fence.enter_context(work_registry.deletion_fence(keys))
        except ChatUnavailable:
            raise HTTPException(status_code=409, detail="Deletion already in progress") from None
        for key in keys:
            stream = await stop_active_stream(key)
            jobs = await stop_workflow_jobs(key)
            if not stream["settled"] or not jobs["settled"]:
                raise HTTPException(status_code=409, detail="Running work did not settle; nothing was deleted")
        async with contextlib.AsyncExitStack() as lanes:
            for key in sorted(set(keys)):
                await lanes.enter_async_context(conversation_stream_lock(key))
            yield


# Long enough for a render to withdraw its queued remote job.
WORKFLOW_STOP_SECS = 10.0


def start_workflow_job(
    cid: str, coro: Coroutine[Any, Any, _T], *, job: str | None = None, message_id: int | None = None
) -> asyncio.Task[_T]:
    """Run *coro* as a job Stop can cancel, unless its resource/source is deleting."""
    try:
        return work_registry.start_job(cid, coro, job=job, message_id=message_id)
    except ChatUnavailable:
        what = "resource" if resource_deleting(cid) else "message"
        raise HTTPException(status_code=409, detail=f"This {what} is being deleted") from None


@contextmanager
def committing_workflow_job() -> Iterator[None]:
    """Stop waits for the current job's final write rather than cancelling it."""
    with work_registry.committing_job():
        yield


async def stop_workflow_jobs(
    cid: str, *, job: str | None = None, message_ids: set[int] | None = None, timeout: float = WORKFLOW_STOP_SECS
) -> dict[str, Any]:
    """Cancel matching renders and await their cleanup; let final writes finish."""
    return await work_registry.stop_jobs(cid, job=job, message_ids=message_ids, timeout=timeout)


@asynccontextmanager
async def locked_attachment_group(aid: int, expected_message_id: int) -> AsyncIterator[tuple[Mapping[str, Any], int]]:
    """Hold the attachment group lock for aid."""
    while True:
        before = await get_workflow_attachment_by_id(aid)
        if before is None or before["message_id"] != expected_message_id:
            raise HTTPException(status_code=404, detail="Attachment not found on this message")
        candidate_root = before["parent_attachment_id"] or before["id"]
        async with _workflow_root_lock(candidate_root):
            current = await get_workflow_attachment_by_id(aid)
            if current is None or current["message_id"] != expected_message_id:
                raise HTTPException(status_code=404, detail="Attachment not found on this message")
            current_root = current["parent_attachment_id"] or current["id"]
            if current_root != candidate_root:
                # A concurrent delete promoted the group's root between the snapshot and this acquire; the lock we hold is for a
                # stale root. Release (exiting this `async with`) and retry on the now-canonical root.
                continue
            yield current, current_root
            return


@asynccontextmanager
async def conversation_stream_lock(cid: str):
    """Queue a history mutation behind the current stream and its settlement."""
    async with work_registry.hold_lane(cid):
        yield


def conversation_busy(cid: str) -> bool:
    """Whether a stream or mutation owns the lane; media may run alongside it."""
    return work_registry.snapshot(cid).lane_busy


@asynccontextmanager
async def stream_idle_lock(cid: str) -> AsyncGenerator[bool, None]:
    """Try to reserve an idle, non-deleting resource's mutation lane."""
    async with contextlib.AsyncExitStack() as stack:
        try:
            await stack.enter_async_context(work_registry.idle_lane(cid))
        except ChatUnavailable:
            yield False
            return
        yield True


@asynccontextmanager
async def idle_chat_admission(cid: str) -> AsyncGenerator[None, None]:
    """Reserve a short mutation's lane, or raise ChatUnavailable without queuing."""
    async with work_registry.idle_lane(cid):
        yield


# Drain a disconnected turn before falling back to cancellation.
_STOP_DRAIN_SECS = 10.0

# Longer than the drain so a disconnected stream can settle inside this wait.
STOP_SETTLE_SECS = 15.0


async def stop_active_stream(
    key: str, *, operation_id: str | None = None, timeout: float = STOP_SETTLE_SECS
) -> dict[str, bool]:
    """Abort this registration and wait for generator cleanup and lane release.

    Later registrations are not awaited. Inactive does not rule out a request
    still in flight, so the client may need to disconnect.
    """
    return await work_registry.stop_stream(key, operation_id=operation_id, timeout=timeout)


async def _safe_aclose(gen: AsyncIterator[Any]) -> None:
    """Close *gen*, shielding the close from cancellation so the generator's own
    finally blocks (e.g. the orchestrator's fallback persistence of incomplete
    messages) always run to completion. If the shield itself is cancelled, retry
    the close once unshielded and log any error."""
    close = getattr(gen, "aclose", None)
    if close is None:
        return
    try:
        await asyncio.shield(close())
    except asyncio.CancelledError:
        try:
            await close()
        except Exception:
            logger.warning("Stream generator failed to close after cancellation", exc_info=True)


class CleanupStreamingResponse(StreamingResponse):
    """Close the body generator on disconnect so pipeline cleanup and fallback saves run.

    Starlette's default StreamingResponse does not close it when send() fails.
    """

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Always close the body generator, even if send() raised (e.g. client disconnected). This ensures the orchestrator's
            # finally block runs and saves any incomplete message.
            if hasattr(self.body_iterator, "aclose"):
                await _safe_aclose(cast(AsyncGenerator[Any, None], self.body_iterator))


# Emit keepalives every 3s during token-free passes. Firefox network-change verification can close connections silent for 5s,
# including loopback; this also stays below common proxy idle timeouts.
_SSE_KEEPALIVE_SECS = 3


async def _drain_after_stop(gen_iter: AsyncIterator[Any], pending: asyncio.Future | None, budget: float) -> None:
    """Drain a stopped generator within *budget*, discarding events.

    Normal settlement preserves completed Editor/workflow work. On timeout,
    cancel the pending step and let the pipeline fallback save the live draft.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while True:
        step = pending if pending is not None else asyncio.ensure_future(gen_iter.__anext__())
        pending = None
        done, _ = await asyncio.wait({step}, timeout=max(0.0, deadline - loop.time()))
        if not done:
            logger.warning("Stopped stream did not wind down within %.0fs; cancelling it", budget)
            step.cancel()
            with contextlib.suppress(BaseException):
                await step
            return
        try:
            step.result()
        except StopAsyncIteration:
            return
        except Exception:
            logger.exception("Stopped stream failed while winding down")
            return


async def _settle_stream(
    gen: AsyncIterator[Any],
    gen_iter: AsyncIterator[Any] | None,
    pending: asyncio.Future | None,
    *,
    finished: bool,
    abort_token: AbortToken | None,
    cid: str | None,
    active: ActiveStream | None,
) -> None:
    """Finish a stream's generator, then give up its registration and lock.

    Runs as its own task: when the request is cancelled (client gone) its awaits would be cancelled too, and the lock must not
    be released while the turn can still write. A queued /edit, /delete or next turn therefore starts only once this reply's
    save has finished.
    """
    try:
        if not finished and gen_iter is not None:
            if abort_token is not None:
                abort_token.abort()
                await _drain_after_stop(gen_iter, pending, _STOP_DRAIN_SECS)
            elif pending is not None:
                pending.cancel()
                with contextlib.suppress(BaseException):
                    await pending
        await _safe_aclose(gen)
    except Exception:
        logger.exception("Stream cleanup failed")
    finally:
        if cid is not None and active is not None:
            work_registry.finish_stream(cid, active)


# Strong references to settle tasks outliving a cancelled request.
_SETTLING: set[asyncio.Task] = set()


def _escape_sse_text(data: str) -> str:
    # Escape the escape character first: a literal backslash-n must remain
    # distinguishable from a newline. Keep CR out of the SSE line grammar too.
    return data.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


async def sse_stream(
    gen: AsyncIterator[PublicEvent | PublicTurnEvent],
    request: Request,
    *,
    abort_token: AbortToken | None = None,
    cid: str | None = None,
) -> AsyncGenerator[str, None]:
    """Encode async events as SSE; a disconnect stops the turn, which still saves."""

    async def _watch_disconnect() -> None:
        try:
            while True:
                if await request.is_disconnected():
                    if abort_token is not None:
                        abort_token.abort()
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    active: ActiveStream | None = None
    watcher: asyncio.Task | None = None
    gen_iter: AsyncIterator[Any] | None = None
    # The step in flight when the request ended, which the settle task takes over.
    pending: asyncio.Future | None = None
    finished = False
    try:
        if cid is not None:
            try:
                active = await work_registry.begin_stream(
                    cid, abort_token, getattr(request, "query_params", {}).get("operation_id")
                )
            except ChatUnavailable as exc:
                detail = (
                    "This resource is being deleted" if exc.reason == "deleting" else "Another generation is already running"
                )
                yield f"event: error\ndata: {detail}\n\n"
                return
        # A Stop that raced the request to the server shows up as a client that
        # has already gone: the turn must not start generating.
        if abort_token is not None and await request.is_disconnected():
            abort_token.abort()
        watcher = asyncio.create_task(_watch_disconnect())
        gen_iter = it = gen.__aiter__()
        while True:
            pending = nxt = asyncio.ensure_future(it.__anext__())
            # Race the next event against the keepalive interval: a silent gap emits a comment frame and keeps waiting on the
            # same task. A cancelled request leaves the task running for the settle task.
            while True:
                done_set, _ = await asyncio.wait({nxt}, timeout=_SSE_KEEPALIVE_SECS)
                if nxt in done_set:
                    break
                yield ": keepalive\n\n"
            pending = None
            try:
                event = nxt.result()
            except StopAsyncIteration:
                finished = True
                break
            except BaseException:
                finished = True
                raise
            if reason := public_event_error(event):
                raise ValueError(f"Invalid public SSE event: {reason}")
            evt_type = event["event"]
            evt_data = event.get("data", "")
            evt_data = json.dumps(evt_data) if isinstance(evt_data, dict) else _escape_sse_text(evt_data)
            yield f"event: {evt_type}\ndata: {evt_data}\n\n"
    except Exception as exc:
        # Headers have already gone out. Generator and encoding failures both
        # need a terminal verdict; settlement still closes the generator and lane.
        finished = True
        yield f"event: error\ndata: {json.dumps(failure_event(exc)['data'])}\n\n"
    finally:
        if watcher is not None:
            watcher.cancel()
        if active is not None:
            work_registry.settling_stream(active)
        settle = asyncio.ensure_future(
            _settle_stream(gen, gen_iter, pending, finished=finished, abort_token=abort_token, cid=cid, active=active)
        )
        _SETTLING.add(settle)
        settle.add_done_callback(_SETTLING.discard)
        await asyncio.shield(settle)


async def _encode_workflow_event_stream(events: AsyncIterator[PublicEvent]) -> AsyncGenerator[str, None]:
    """Encode workflow events as SSE."""
    try:
        it = events.__aiter__()
        while True:
            nxt = asyncio.ensure_future(it.__anext__())
            try:
                # Same keepalive race as sse_stream: a long silent ComfyUI render yields no labels for stretches, so emit
                # comment frames to keep an idle-timeout proxy/browser from dropping the stream (which surfaced as a frontend
                # "Error in input stream" while the backend rendered on and persisted the image unseen).
                while True:
                    done_set, _ = await asyncio.wait({nxt}, timeout=_SSE_KEEPALIVE_SECS)
                    if nxt in done_set:
                        break
                    yield ": keepalive\n\n"
            except BaseException:
                # Let the workflow's own cleanup (withdrawing a queued render)
                # finish first: closing a generator mid-step raises instead.
                nxt.cancel()
                await asyncio.wait({nxt})
                raise
            try:
                ev = nxt.result()
            except StopAsyncIteration:
                break
            reason = public_event_error(ev)
            if reason is not None:
                logger.warning("workflow on-demand stream yielded an invalid public event (%s); dropping", reason)
                continue
            name = ev["event"]
            data = ev.get("data", "")
            data = json.dumps(data, separators=(",", ":")) if isinstance(data, dict) else _escape_sse_text(data)
            yield f"event: {name}\ndata: {data}\n\n"
    except Exception as exc:
        yield f"event: error\ndata: {json.dumps(failure_event(exc)['data'])}\n\n"
    finally:
        if hasattr(events, "aclose"):
            await _safe_aclose(cast(AsyncGenerator[Any, None], events))


def workflow_event_stream_response(
    stream: WorkflowEventStream, *, cid: str | None = None, job: str | None = None, message_id: int | None = None
) -> CleanupStreamingResponse:
    """Keep lazy on-demand renders registered until their generator settles.

    Without *cid* the caller already runs the work as a job, so the frames are encoded straight through.
    """
    if cid is None:
        return CleanupStreamingResponse(_encode_workflow_event_stream(stream.events), media_type="text/event-stream")

    async def tracked():
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def consume():
            try:
                async for frame in _encode_workflow_event_stream(stream.events):
                    queue.put_nowait(frame)
            finally:
                queue.put_nowait(None)

        task = start_workflow_job(cid, consume(), job=job, message_id=message_id)
        try:
            while (frame := await queue.get()) is not None:
                yield frame
        finally:
            work_registry.cancel_job(task)
            with contextlib.suppress(asyncio.CancelledError):
                # Request cancellation must not interrupt remote cleanup again;
                # the registry retains the job until it actually finishes.
                await asyncio.shield(task)

    return CleanupStreamingResponse(tracked(), media_type="text/event-stream")


def pipeline_sse_response(
    make_gen: Callable[[AbortToken], AsyncIterator[PublicTurnEvent | PublicEvent]], request: Request, cid: str
) -> CleanupStreamingResponse:
    """Standard SSE response for a turn-lifecycle event generator.

    *make_gen* receives a fresh :class:`AbortToken` and returns the event generator; the same token is registered with the
    stream so POST /stop can signal it.
    """
    abort_token = AbortToken()
    return CleanupStreamingResponse(
        sse_stream(make_gen(abort_token), request, abort_token=abort_token, cid=cid), media_type="text/event-stream"
    )


def rows_response(rows: Sequence[Mapping[str, Any]]) -> JSONResponse:
    """Render already JSON-compatible database rows without jsonable_encoder's copy pass."""
    return JSONResponse(rows)


_IMAGE_CACHE_CONTROL = "private, max-age=300"
_INERT_CONTENT_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}


def image_not_modified(etag: str, request: Request) -> Response | None:
    """A 304 when the client already holds *etag*, else None."""
    if request.headers.get("if-none-match") != etag:
        return None
    return Response(status_code=304, headers={**_INERT_CONTENT_HEADERS, "Cache-Control": _IMAGE_CACHE_CONTROL, "ETag": etag})


def cached_image_response(image_bytes: bytes, mime: str | None, request: Request, etag: str | None = None) -> Response:
    """Return a privately cacheable image response with ETag support.

    Without *etag* the tag is a hash of the bytes. A caller that can version the image more cheaply passes its own tag, and can
    check it with :func:`image_not_modified` before loading the bytes at all. Imported bytes and MIME types are untrusted: a
    direct navigation must not execute HTML or SVG scripts on the app's origin.
    """
    etag = etag or '"' + hashlib.md5(image_bytes, usedforsecurity=False).hexdigest() + '"'
    not_modified = image_not_modified(etag, request)
    if not_modified is not None:
        return not_modified
    headers = {**_INERT_CONTENT_HEADERS, "Cache-Control": _IMAGE_CACHE_CONTROL, "ETag": etag}
    return Response(content=image_bytes, media_type=mime or "image/png", headers=headers)


# The MIME shape the frontend's ATTACHMENT_MIME_RE accepts (frontend/utils.js).
_ATTACHMENT_MIME_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", re.IGNORECASE)
_BYTE_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def attachment_content_response(data_b64: str, mime: str | None, request: Request) -> Response:
    """Serve attachment bytes with ETag revalidation and byte ranges.

    Use no-cache because rehydration can change bytes under the same id. Ranges support media seeking. Invalid MIME types become
    opaque bytes; nosniff and a sandbox CSP keep stored HTML inert when opened directly.
    """
    try:
        data = base64.b64decode(data_b64)
    except binascii.Error:
        raise HTTPException(status_code=422, detail="Attachment bytes are not valid base64") from None
    etag = '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'
    headers = {
        "Cache-Control": "private, no-cache",
        "ETag": etag,
        "Accept-Ranges": "bytes",
        **_INERT_CONTENT_HEADERS,
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    declared = (mime or "").strip()
    media_type = declared.lower() if _ATTACHMENT_MIME_RE.fullmatch(declared) else "application/octet-stream"
    size = len(data)
    # A Range this does not understand (several ranges, another unit, a reversed span) is ignored and answered with the whole
    # body, as RFC 9110 allows. If-Range with a stale validator means the same.
    match = _BYTE_RANGE_RE.fullmatch((request.headers.get("range") or "").strip())
    fresh = request.headers.get("if-range") in (None, etag)
    if match and fresh and (match[1] or match[2]):
        if match[1]:
            start = int(match[1])
            end = min(int(match[2]), size - 1) if match[2] else size - 1
            reversed_span = bool(match[2]) and int(match[2]) < start
        else:
            start, end, reversed_span = max(size - int(match[2]), 0), size - 1, False
        if not reversed_span:
            if start >= size or (not match[1] and int(match[2]) == 0):
                return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
            return Response(
                content=data[start : end + 1],
                status_code=206,
                media_type=media_type,
                headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"},
            )
    return Response(content=data, media_type=media_type, headers=headers)


async def require_conversation(cid: str) -> ConversationRow:
    """404 guard shared by the ``/api/conversations/{cid}/...`` routes."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


@contextmanager
def profile_draft_failures(what: str):
    """Map profile-drafting errors to HTTP responses."""
    try:
        yield
    except ProfileDraftUnavailable as exc:
        logger.warning("%s: %s", what, exc)
        raise HTTPException(status_code=502, detail=str(exc)) from None
    except API_PASSTHROUGH_ERRORS:
        raise
    except Exception:
        logger.exception("%s", what)
        raise HTTPException(status_code=500, detail="Profile drafting failed; see server logs") from None


async def require_world(world_id: str) -> Mapping[str, Any]:
    world = await get_world(world_id)
    if not world:
        raise HTTPException(status_code=404, detail="World not found")
    return world


async def require_lorebook_entry(entry_id: int, world: dict = Depends(require_world)) -> Mapping[str, Any]:  # noqa: B008
    entry = await get_lorebook_entry(entry_id)
    if not entry or entry.get("world_id") != world["id"]:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


async def require_changeset(changeset_id: int, world: dict = Depends(require_world)) -> Mapping[str, Any]:  # noqa: B008
    """Load a world changeset, scoped to the World in the path.

    Scoped rather than looked up by bare id so a changeset id from one World can never be decided through another World's route.
    """
    changeset = await get_world_changeset(changeset_id)
    if not changeset or changeset.get("world_id") != world["id"]:
        raise HTTPException(status_code=404, detail="Changeset not found")
    return changeset


def validate_phrase_group(kind: str, variants: list[str], pattern: str) -> tuple[list[str], str]:
    """Validate a phrase group by kind. Returns (variants, pattern) to persist.

    A group is *either* literal variants *or* a single regex -- never both.
    """
    if kind == "regex":
        pattern = (pattern or "").strip()
        if not pattern:
            raise HTTPException(status_code=400, detail="A regex pattern is required")
        if len(pattern) > MAX_PHRASE_REGEX:
            raise HTTPException(status_code=400, detail=f"Pattern must be {MAX_PHRASE_REGEX} characters or less")
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regular expression: {e}") from e
        # Regex groups carry no literal variants.
        return [], pattern

    # Literal group.
    cleaned = [v.strip() for v in (variants or []) if isinstance(v, str) and v.strip()]
    if not cleaned:
        raise HTTPException(status_code=400, detail="At least one variant is required")
    return cleaned, ""
