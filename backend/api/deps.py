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
from collections.abc import (
    AsyncGenerator,
    AsyncIterator,
    Callable,
    Coroutine,
    Iterator,
    Mapping,
    Sequence,
)
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any, TypeVar, cast

import httpx
from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..database import (
    get_conversation,
    get_lorebook_entry,
    get_workflow_attachment_by_id,
    get_world,
    get_world_changeset,
)
from ..database.models import ConversationRow
from ..features import lorebook
from ..features.cards import ProfileDraftUnavailable
from ..inference import AbortToken, LLMCallError, provider_sentence
from ..workflows import WorkflowEventStream, public_event_error

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend")


# Per-root_id serialization for mutations of workflow_attachments groups.
# Regenerate, reroll-gen, rehydrate, and delete all mutate the sibling tree.
# BEGIN IMMEDIATE prevents data corruption, but commit order across
# concurrent transactions is indeterminate; the loser's API response can name
# a sibling whose active-pointer status the winner has already overwritten.
# The lock turns concurrent requests into sequential ones so the loser
# proceeds against post-winner state. /activate stays out of it: these holders
# run for the whole render, and queuing a swipe behind one blocks artifact
# navigation for its duration (see api_activate_workflow_attachment).
#
# Dict grows over the process lifetime, bounded by distinct root_ids the
# user has interacted with. Single-user localhost app, so cap is small
# and process restart resets. dict.setdefault is a single CPython
# bytecode with no await between read and write, so no guard lock is
# needed around the dict itself.
_workflow_root_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def _workflow_root_lock(root_id: int):
    lock = _workflow_root_locks.setdefault(root_id, asyncio.Lock())
    async with lock:
        yield


def workflow_group_in_flight(root_id: int) -> bool:
    """Whether a request currently holds this group's root lock.

    Every operation that can add or remove a sibling holds the lock for its
    whole duration and releases it only after its write commits, so False means
    nothing in flight can still change this group -- the question a client is
    left with when its own request dies on the wire mid-render.

    False also covers "queued, but not yet at the lock", so a caller must see it
    more than once before treating an operation as over.
    """
    lock = _workflow_root_locks.get(root_id)
    return lock is not None and lock.locked()


# Workflow renders Stop can cancel, by conversation. Regenerate and reroll-gen
# deliberately outlive a dropped connection, so closing it cannot stop them;
# they run as tasks here instead, each under the job id its client named (or
# None), so the button that started one can stop that render alone.
_workflow_jobs: dict[str, dict[asyncio.Task[Any], str | None]] = {}
_workflow_sources: dict[asyncio.Task[Any], int | None] = {}
_deleting_resources: set[str] = set()
_deleting_messages: set[int] = set()


def require_resource_available(cid: str) -> None:
    if cid in _deleting_resources:
        raise HTTPException(status_code=409, detail="This resource is being deleted")


def require_chats_idle(rows: Sequence[Mapping[str, Any]]) -> None:
    for row in rows:
        cid = str(row["id"])
        lane = _conversation_stream_locks.get(cid)
        if cid in _active_streams or _workflow_jobs.get(cid) or (lane is not None and lane.locked()):
            raise HTTPException(
                status_code=409,
                detail=f"Stop running work in {row.get('title') or cid} first",
            )


@contextmanager
def idle_chats_guard(rows: Sequence[Mapping[str, Any]]):
    """Refuse busy chats and fence admission through a destructive write."""
    require_chats_idle(rows)
    added = {str(row["id"]) for row in rows} - _deleting_resources
    _deleting_resources.update(added)
    try:
        yield
    finally:
        _deleting_resources.difference_update(added)


@asynccontextmanager
async def deleting_message_sources(cid: str, message_ids: set[int]):
    _deleting_messages.update(message_ids)
    try:
        result = await stop_workflow_jobs(cid, message_ids=message_ids)
        if not result["settled"]:
            raise HTTPException(status_code=409, detail="Media did not settle; nothing was deleted")
        yield
    finally:
        _deleting_messages.difference_update(message_ids)


def active_work() -> list[str]:
    return [f"Generation in {key}" for key in _active_streams] + [
        f"Media in {cid}" for cid, jobs in _workflow_jobs.items() if jobs
    ]


@asynccontextmanager
async def deleting_resources(keys: Sequence[str]):
    """Close admission, settle work, then take lanes for the final delete."""
    if any(key in _deleting_resources for key in keys):
        raise HTTPException(status_code=409, detail="Deletion already in progress")
    _deleting_resources.update(keys)
    try:
        for key in keys:
            stream = await stop_active_stream(key)
            jobs = await stop_workflow_jobs(key)
            if not stream["settled"] or not jobs["settled"]:
                raise HTTPException(
                    status_code=409,
                    detail="Running work did not settle; nothing was deleted",
                )
        async with contextlib.AsyncExitStack() as stack:
            for key in sorted(set(keys)):
                await stack.enter_async_context(_conversation_stream_lock(key))
            yield
    finally:
        _deleting_resources.difference_update(keys)


# Jobs inside their final write. Stop waits for these rather than cancelling:
# a cancelled await does not stop SQLite's worker thread from committing.
_committing_jobs: set[asyncio.Task[Any]] = set()

# How long a workflow Stop waits for its jobs to wind down; long enough for a
# render to withdraw its queued remote job.
WORKFLOW_STOP_SECS = 10.0


def start_workflow_job(
    cid: str,
    coro: Coroutine[Any, Any, _T],
    *,
    job: str | None = None,
    message_id: int | None = None,
) -> asyncio.Task[_T]:
    """Run *coro* as a job that `stop_workflow_jobs(cid)` can cancel, filed under *job*."""
    if cid in _deleting_resources or message_id in _deleting_messages:
        coro.close()
        what = "resource" if cid in _deleting_resources else "message"
        raise HTTPException(status_code=409, detail=f"This {what} is being deleted")
    task = asyncio.create_task(coro)
    jobs = _workflow_jobs.setdefault(cid, {})
    jobs[task] = job
    _workflow_sources[task] = message_id

    def forget(done: asyncio.Task[Any]) -> None:
        jobs.pop(done, None)
        _workflow_sources.pop(done, None)
        if not jobs and _workflow_jobs.get(cid) is jobs:
            del _workflow_jobs[cid]

    task.add_done_callback(forget)
    return task


@contextmanager
def committing_workflow_job() -> Iterator[None]:
    """Mark the current job as writing its result, which Stop lets finish."""
    task = asyncio.current_task()
    if task is None:
        yield
        return
    _committing_jobs.add(task)
    try:
        yield
    finally:
        _committing_jobs.discard(task)


async def stop_workflow_jobs(
    cid: str,
    *,
    job: str | None = None,
    message_ids: set[int] | None = None,
    timeout: float = WORKFLOW_STOP_SECS,
) -> dict[str, Any]:
    """Cancel the conversation's workflow jobs, or only those filed under *job*,
    and wait, bounded, for them to end.

    A job already writing its result is waited for, not cancelled: its
    sibling lands, and the client's refetch shows it.
    """
    jobs = [
        task
        for task, name in _workflow_jobs.get(cid, {}).items()
        if (job is None or name == job) and (message_ids is None or _workflow_sources.get(task) in message_ids)
    ]
    if not jobs:
        return {"stopped": 0, "settled": True}
    for task in jobs:
        if task not in _committing_jobs:
            task.cancel()
    _, pending = await asyncio.wait(jobs, timeout=timeout)
    return {"stopped": len(jobs), "settled": not pending}


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
                # A concurrent delete promoted the group's root between the
                # snapshot and this acquire; the lock we hold is for a stale
                # root. Release (exiting this `async with`) and retry on the
                # now-canonical root.
                continue
            yield current, current_root
            return


# One large download at a time, across every route that starts one. The
# local-ML model fetches and the llama-server runtime fetch are separate
# routers but the same resource: a single-user box on a home connection, where
# two multi-gigabyte pulls at once are slower than either alone and the runtime
# fetch also replaces a directory a model load may be reading from. Lives here
# rather than in a route module because it is shared mutable state and two
# routers must bind the same object.
_download_lock = asyncio.Lock()


# Per-conversation serialization for the streaming pipeline. The five chat
# streaming routes refuse a second POST against a held lock with an in-band
# SSE error event; /edit, /delete, and /switch-branch share the same lock
# but block on the stream instead of erroring, since they have no SSE
# channel for an "already running" reply and the user expects them to take
# effect rather than fail. The lock prevents doubled-LLM cost on concurrent
# /send, FK cascade on mid-stream /delete, terminal set_active_leaf clobber
# of a mid-stream /switch-branch, and pre-edit-prefix vs post-edit-DB skew on
# mid-stream /edit. Dict growth shape matches _workflow_root_locks.
_conversation_stream_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def _conversation_stream_lock(cid: str):
    lock = _conversation_stream_locks.setdefault(cid, asyncio.Lock())
    async with lock:
        yield


@asynccontextmanager
async def stream_idle_lock(cid: str) -> AsyncGenerator[bool, None]:
    """Hold the stream lock until request cleanup."""
    lock = _conversation_stream_locks.setdefault(cid, asyncio.Lock())
    if lock.locked():
        yield False
        return
    await lock.acquire()
    try:
        yield True
    finally:
        lock.release()


@dataclass(slots=True)
class _ActiveStream:
    """The stream that currently owns a conversation's lock.

    One token covers every client in the turn (writer + optional agent), so
    /stop signals them all at once. ``settled`` is set only after the generator
    has finished, including its persistence, and the lock has been released.
    """

    abort_token: AbortToken
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    operation_id: str | None = None


# Keyed like the stream lock. Set when streaming starts; removed when the stream
# has settled.
_active_streams: dict[str, _ActiveStream] = {}

# How long a stopped stream whose client has gone away may take to wind down on
# its own before it is cancelled. Every stage checks the abort token, so this is
# a bound for a misbehaving one, not the expected wait.
_STOP_DRAIN_SECS = 10.0

# How long POST /stop waits for the stopped stream to settle. Longer than the
# drain, so a disconnected stream settles inside it.
STOP_SETTLE_SECS = 15.0


async def stop_active_stream(
    key: str, *, operation_id: str | None = None, timeout: float = STOP_SETTLE_SECS
) -> dict[str, bool]:
    """Abort the stream registered under *key* and wait, bounded, for it to settle.

    ``active`` says whether a stream was registered when the request arrived;
    ``settled`` that it has since finished saving and released the lock. A
    stream registered after this call is not waited on: it is a new operation.
    Returning ``active: False`` is not proof that a request still in flight can
    never start, which is why the client falls back to dropping its connection.
    """
    active = _active_streams.get(key)
    if active is None or (operation_id is not None and active.operation_id != operation_id):
        return {"active": False, "settled": True}
    active.abort_token.abort()
    try:
        await asyncio.wait_for(active.settled.wait(), timeout)
    except TimeoutError:
        return {"active": True, "settled": False}
    return {"active": True, "settled": True}


async def _safe_aclose(gen: AsyncGenerator[Any, None]) -> None:
    """Close *gen*, shielding the close from cancellation so the generator's own
    finally blocks (e.g. the orchestrator's fallback persistence of incomplete
    messages) always run to completion. If the shield itself is cancelled, retry
    the close once unshielded and swallow any error."""
    try:
        await asyncio.shield(gen.aclose())
    except asyncio.CancelledError:
        try:
            await gen.aclose()
        except Exception:
            pass


class _CleanupStreamingResponse(StreamingResponse):
    """StreamingResponse that guarantees the body async generator is closed
    even when the client disconnects mid-stream.

    Starlette's default StreamingResponse does NOT close the body iterator
    when send() fails due to client disconnect. This subclass ensures proper
    cleanup so that orchestrator finally blocks (which save incomplete messages
    on abort) always execute.
    """

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Always close the body generator, even if send() raised
            # (e.g. client disconnected). This ensures the orchestrator's
            # finally block runs and saves any incomplete message.
            if hasattr(self.body_iterator, "aclose"):
                await _safe_aclose(cast(AsyncGenerator[Any, None], self.body_iterator))


# Seconds of stream silence after which we emit an SSE comment to keep the
# connection warm. A turn has long token-free stretches — the reasoning-off
# director pass, and (worst) the text-mode editor's prefill loop, which fires
# many forced /completion calls back-to-back while emitting nothing to the
# browser. Two separate timers kill a silent stream, and the shorter one sets
# this value:
#
# 1. The browser. When the OS reports a network change (on Linux, a
#    NetworkManager state change over D-Bus), Firefox re-verifies traffic on
#    every *active* connection and closes the ones that moved zero bytes inside
#    network.http.network-changed.timeout — 5s by default — with
#    NS_ERROR_NET_RESET. Loopback is not exempt. The page sees its fetch body
#    fail ("Error in input stream"), never a clean end, so the turn dies with
#    nothing logged on this side. A DHCPv6 lease renewal is such a change, and a
#    router handing out a short T1 fires one every minute: a 15s heartbeat left
#    gaps wide enough that turns died mid-generation several times an hour.
# 2. An idle-timeout proxy in front of Orb (nginx proxy_read_timeout defaults to
#    60s) tears down the same silent SSE, which strands the still-running
#    backend and drops the frontend to a stale draft.
#
# 3s keeps every gap inside the browser's verification window and far under
# common proxy timeouts, for ~4 bytes/s on an otherwise idle stream.
_SSE_KEEPALIVE_SECS = 3


async def _drain_after_stop(gen_iter: AsyncIterator[Any], pending: asyncio.Future | None, budget: float) -> None:
    """Run a stopped generator to its end, discarding events, within *budget* seconds.

    Its client is gone, but the turn still has to save what it accepted. Every
    stage checks the abort token and winds down on its own; running it out lets
    the normal save keep the Editor's and workflows' finished work, which
    cancelling it mid-stage would lose. Past the budget the pending step is
    cancelled, and the pipeline's fallback save keeps the live draft instead.
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
    gen: AsyncGenerator[Any, None],
    gen_iter: AsyncIterator[Any] | None,
    pending: asyncio.Future | None,
    *,
    finished: bool,
    abort_token: AbortToken | None,
    cid: str | None,
    active: _ActiveStream | None,
    lock: asyncio.Lock | None,
) -> None:
    """Finish a stream's generator, then give up its registration and lock.

    Runs as its own task: when the request is cancelled (client gone) its
    awaits would be cancelled too, and the lock must not be released while the
    turn can still write. A queued /edit, /delete or next turn therefore starts
    only once this reply's save has finished.
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
        if cid is not None and active is not None and _active_streams.get(cid) is active:
            del _active_streams[cid]
        if lock is not None:
            lock.release()
        if active is not None:
            active.settled.set()


# Strong references to settle tasks outliving a cancelled request.
_SETTLING: set[asyncio.Task] = set()


async def _sse_stream(
    gen,
    request: Request,
    *,
    abort_token: AbortToken | None = None,
    cid: str | None = None,
):
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

    lock: asyncio.Lock | None = None
    active: _ActiveStream | None = None
    watcher: asyncio.Task | None = None
    gen_iter: AsyncIterator[Any] | None = None
    # The step in flight when the request ended, which the settle task takes over.
    pending: asyncio.Future | None = None
    finished = False
    try:
        if cid is not None:
            if cid in _deleting_resources:
                yield "event: error\ndata: This resource is being deleted\n\n"
                return
            # locked()/acquire() are atomic across coroutines (no await between)
            # so a held-lock loser deterministically takes the error branch and
            # does not queue, while an open-lock winner acquires without ever
            # suspending. Lock is set only after acquire() returns so the
            # finally's release guard skips both the error branch and any
            # acquire-cancelled path.
            candidate = _conversation_stream_locks.setdefault(cid, asyncio.Lock())
            if candidate.locked():
                yield "event: error\ndata: Another generation is already running\n\n"
                return
            await candidate.acquire()
            lock = candidate
            # Register only after winning the lock, so a rejected loser never
            # clobbers the winner's entry.
            if abort_token is not None:
                active = _ActiveStream(
                    abort_token,
                    operation_id=getattr(request, "query_params", {}).get("operation_id"),
                )
                _active_streams[cid] = active
        # A Stop that raced the request to the server shows up as a client that
        # has already gone: the turn must not start generating.
        if abort_token is not None and await request.is_disconnected():
            abort_token.abort()
        watcher = asyncio.create_task(_watch_disconnect())
        gen_iter = it = gen.__aiter__()
        while True:
            pending = nxt = asyncio.ensure_future(it.__anext__())
            # Race the next event against the keepalive interval: a silent gap
            # emits a comment frame and keeps waiting on the same task. A
            # cancelled request leaves the task running for the settle task.
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
            evt_type = event["event"]
            evt_data = event.get("data", "")
            if isinstance(evt_data, dict):
                evt_data = json.dumps(evt_data)
            elif isinstance(evt_data, str):
                evt_data = evt_data.replace("\n", "\\n")
            yield f"event: {evt_type}\ndata: {evt_data}\n\n"
    finally:
        if watcher is not None:
            watcher.cancel()
        settle = asyncio.ensure_future(
            _settle_stream(
                gen,
                gen_iter,
                pending,
                finished=finished,
                abort_token=abort_token,
                cid=cid,
                active=active,
                lock=lock,
            )
        )
        _SETTLING.add(settle)
        settle.add_done_callback(_SETTLING.discard)
        await asyncio.shield(settle)


async def _encode_workflow_event_stream(events: AsyncIterator[dict]) -> AsyncGenerator[str, None]:
    """Encode workflow events as SSE."""
    try:
        it = events.__aiter__()
        while True:
            nxt = asyncio.ensure_future(it.__anext__())
            try:
                # Same keepalive race as _sse_stream: a long silent ComfyUI
                # render yields no labels for stretches, so emit comment frames
                # to keep an idle-timeout proxy/browser from dropping the stream
                # (which surfaced as a frontend "Error in input stream" while the
                # backend rendered on and persisted the image unseen).
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
            if isinstance(data, dict):
                data = json.dumps(data, separators=(",", ":"))
            else:
                data = data.replace("\n", "\\n")
            yield f"event: {name}\ndata: {data}\n\n"
    finally:
        if hasattr(events, "aclose"):
            await _safe_aclose(cast(AsyncGenerator[Any, None], events))


def _workflow_event_stream_response(
    stream: WorkflowEventStream,
    *,
    cid: str | None = None,
    job: str | None = None,
    message_id: int | None = None,
) -> _CleanupStreamingResponse:
    """Keep lazy on-demand renders registered until their generator settles.

    Without *cid* the caller already runs the work as a job, so the frames are
    encoded straight through.
    """
    if cid is None:
        return _CleanupStreamingResponse(_encode_workflow_event_stream(stream.events), media_type="text/event-stream")

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
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return _CleanupStreamingResponse(tracked(), media_type="text/event-stream")


def _pipeline_sse_response(
    make_gen: Callable[[AbortToken], AsyncIterator[Any]],
    request: Request,
    cid: str,
) -> _CleanupStreamingResponse:
    """Standard SSE response for a turn-lifecycle event generator.

    *make_gen* receives a fresh :class:`AbortToken` and returns the event
    generator; the same token is registered with the stream so POST /stop can
    signal it.
    """
    abort_token = AbortToken()
    return _CleanupStreamingResponse(
        _sse_stream(make_gen(abort_token), request, abort_token=abort_token, cid=cid),
        media_type="text/event-stream",
    )


# What a transport failure says when the provider gave us no words of its own.
_PROFILE_UPSTREAM = "The model endpoint did not answer the profile request."


def rows_response(rows: Sequence[Mapping[str, Any]]) -> JSONResponse:
    """Render database rows as JSON without FastAPI's ``jsonable_encoder`` pass.

    A route without a response model has its return value walked by
    ``jsonable_encoder``, which rebuilds every dict and list. Rows read from
    SQLite and decoded with ``json.loads`` already hold only JSON types, so the
    walk changes nothing, and on the large listings it costs more than
    rendering: 16 ms of the 958-conversation list's 55 ms.
    """
    return JSONResponse(rows)


_IMAGE_CACHE_CONTROL = "private, max-age=300"


def image_not_modified(etag: str, request: Request) -> Response | None:
    """A 304 when the client already holds *etag*, else None."""
    if request.headers.get("if-none-match") != etag:
        return None
    return Response(status_code=304, headers={"Cache-Control": _IMAGE_CACHE_CONTROL, "ETag": etag})


def cached_image_response(image_bytes: bytes, mime: str | None, request: Request, etag: str | None = None) -> Response:
    """Return a privately cacheable image response with ETag support.

    Without *etag* the tag is a hash of the bytes. A caller that can version the
    image more cheaply passes its own tag, and can check it with
    :func:`image_not_modified` before loading the bytes at all.
    """
    etag = etag or '"' + hashlib.md5(image_bytes, usedforsecurity=False).hexdigest() + '"'
    not_modified = image_not_modified(etag, request)
    if not_modified is not None:
        return not_modified
    headers = {"Cache-Control": _IMAGE_CACHE_CONTROL, "ETag": etag}
    return Response(content=image_bytes, media_type=mime or "image/png", headers=headers)


# The MIME shape the frontend's ATTACHMENT_MIME_RE accepts (frontend/utils.js).
_ATTACHMENT_MIME_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*", re.IGNORECASE)
_BYTE_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


def attachment_content_response(data_b64: str, mime: str | None, request: Request) -> Response:
    """Serve one stored attachment's bytes, revalidated by ETag, with byte ranges.

    The message listing carries attachments without their bytes; each one's
    bytes load from here. ``no-cache`` rather than a max-age because rehydrate
    refills an evicted row with regenerated bytes under the same id, so a cached
    copy may be reused only once the server confirms it still matches. Ranges
    because media elements seek with them, and Safari will not play video
    without them.

    The MIME type is whatever the client or workflow stored. An ill-formed one
    is served as opaque bytes, and ``nosniff`` plus a sandbox CSP keep a stored
    ``text/html`` payload inert if the URL is opened directly.
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
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    }
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    declared = (mime or "").strip()
    media_type = declared.lower() if _ATTACHMENT_MIME_RE.fullmatch(declared) else "application/octet-stream"
    size = len(data)
    # A Range this does not understand (several ranges, another unit, a
    # reversed span) is ignored and answered with the whole body, as RFC 9110
    # allows. If-Range with a stale validator means the same.
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
    except LLMCallError as exc:
        logger.warning("%s: %s", what, exc.sentence or exc)
        raise HTTPException(status_code=502, detail=exc.sentence or _PROFILE_UPSTREAM) from None
    except httpx.HTTPStatusError as exc:
        sentence = provider_sentence(exc.response.text if exc.response is not None else "")
        logger.warning("%s: %s", what, sentence or exc)
        raise HTTPException(status_code=502, detail=sentence or _PROFILE_UPSTREAM) from None
    except httpx.HTTPError as exc:
        logger.warning("%s: %s", what, exc)
        raise HTTPException(status_code=502, detail=_PROFILE_UPSTREAM) from None
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

    Scoped rather than looked up by bare id so a changeset id from one World can
    never be decided through another World's route.
    """
    changeset = await get_world_changeset(changeset_id)
    if not changeset or changeset.get("world_id") != world["id"]:
        raise HTTPException(status_code=404, detail="Changeset not found")
    return changeset


def project_lorebook_view(entries: Sequence[Mapping[str, Any]], view: str) -> list[Mapping[str, Any]]:
    """Project a World into the requested lorebook view."""
    if view == "authored":
        return [e for e in entries if e.get("entry_layer") != "dynamic"]
    return list(lorebook.select_effective_entries(entries)) if view == "effective" else list(entries)


# A V3 entry may open with decorator lines (`@@depth 4`, `@@@fallback`, …).
_DECORATOR_PREAMBLE = re.compile(r"\A\s*(?:@@[^\n]*\n?)+")


def _strip_decorators(content: str) -> str:
    """Drop the V3 decorator preamble from an entry's content."""
    stripped = _DECORATOR_PREAMBLE.sub("", content)
    return stripped.lstrip("\n") if stripped != content else content


def _str_list(value: Any) -> list[str]:
    return [str(k) for k in value if k] if isinstance(value, list) else []


def _normalise_lorebook_entry(item: dict) -> dict:
    keywords = _str_list(item.get("keys") or item.get("key") or [])
    secondary_keys = _str_list(item.get("secondary_keys") or item.get("keysecondary") or [])
    name = item.get("name") or item.get("comment") or ""
    if "disable" in item:
        enabled = not item["disable"]
    else:
        enabled = bool(item.get("enabled", True))
    priority = int(item.get("priority") or item.get("insertion_order") or item.get("order") or 100)
    # A standalone World Info file keeps its non-V2 entry fields at the top
    # level; a card-embedded `character_book` parks the same fields under
    # `extensions` (position, depth, case_sensitive, …). Read both spellings
    # so either export lands intact.
    raw_ext = item.get("extensions")
    ext: dict = raw_ext if isinstance(raw_ext, dict) else {}
    case_sensitive = item.get("caseSensitive") or item.get("case_sensitive") or ext.get("case_sensitive")
    constant = bool(item.get("constant", False))
    return {
        "name": str(name),
        "content": _strip_decorators(str(item.get("content") or "")),
        "keywords": keywords,
        "enabled": enabled,
        "priority": priority,
        # `priority` keeps its own fallback chain above (rewriting it would
        # reshuffle already-imported V2 books); sort_order carries the spec field.
        "sort_order": int(item.get("insertion_order") or 0),
        "case_insensitive": not bool(case_sensitive),
        "constant": constant,
        # World Info's `position: 4` is "@ Depth" — injected after the latest
        # message instead of into the character defs. V2/V3 `character_book`
        # spells the top-level position as a string ("before_char"/"after_char"),
        # which is never 4; the numeric one lives in `extensions`. `at_depth` is
        # our own export key, read back so an Orb round-trip is lossless.
        "at_depth": bool(item.get("at_depth")) or 4 in (item.get("position"), ext.get("position")),
        "use_regex": bool(item.get("use_regex", False)),
        # Cards in the wild set `selective` on every entry while leaving
        # secondary_keys empty; honouring that literally would make the whole
        # book match nothing, so an unbacked flag stores as false.
        "selective": bool(item.get("selective")) and bool(secondary_keys),
        "secondary_keys": secondary_keys,
    }


def lorebook_to_book(
    world_name: str,
    entries: Sequence[Mapping[str, Any]],
    *,
    dynamic_enabled: bool = False,
) -> dict[str, Any]:
    """Serialize a World lorebook to Character Card shape."""
    return {
        "name": world_name,
        # Orb's own marker. It round-trips the Dynamic World flag, and its mere
        # presence tells the importer the book is a World Orb exported — so an
        # entry-less one is a real lorebook to restore (a Dynamic World starts
        # empty by design) rather than the vestigial `entries: []` that foreign
        # cards carry.
        "extensions": {"orb": {"dynamic_enabled": bool(dynamic_enabled)}},
        "entries": [
            {
                "keys": e["keywords"],
                "content": e["content"],
                # World Info readers take placement and case-sensitivity from
                # here, not from the V2 top-level keys — without this block a
                # round-trip drops @ Depth and the case flag. `depth: 0` is where
                # Orb puts the block: immediately after the latest message.
                "extensions": {
                    "position": 4 if e.get("at_depth") else 1,
                    "depth": 0,
                    "case_sensitive": not bool(e["case_insensitive"]),
                },
                "position": "after_char",
                "enabled": bool(e["enabled"]),
                "insertion_order": e["sort_order"],
                "case_sensitive": not bool(e["case_insensitive"]),
                "constant": bool(e.get("constant", False)),
                # Additive: our own spelling of the depth flag, read back on import.
                "at_depth": bool(e.get("at_depth", False)),
                "name": e["name"],
                # World Info readers title an entry from `comment`; `name` is the
                # V2 spelling. Both, so either reader shows the title.
                "comment": e["name"],
                "priority": e["priority"],
                "id": e["id"],
                "use_regex": bool(e.get("use_regex", False)),
                "selective": bool(e.get("selective", False)),
                "secondary_keys": e.get("secondary_keys") or [],
            }
            for e in entries
        ],
    }


def _validate_phrase_group(kind: str, variants: list[str], pattern: str) -> tuple[list[str], str]:
    """Validate a phrase group by kind. Returns (variants, pattern) to persist.

    A group is *either* literal variants *or* a single regex — never both.
    """
    if kind == "regex":
        pattern = (pattern or "").strip()
        if not pattern:
            raise HTTPException(status_code=400, detail="A regex pattern is required")
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
