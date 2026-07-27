"""Cross-route shared state and helpers for the ``api/`` layer.

Everything here is process-global surface that more than one route module
needs, plus the few ``Depends`` providers and request validators the route
files share. It lives in exactly one module so the shared mutable registries
(``_active_aborts`` and the lock dicts) are never duplicated when the routes
are split across files -- importing a name binds the one canonical object.

Patch seam (mirrors the ``DB_PATH`` trap in ``database/connection.py``):
``_workflow_root_locks`` / ``_conversation_stream_locks`` are patched in tests
via ``backend.api.deps`` -- the canonical module, not a facade re-export.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from ..database import (
    get_conversation,
    get_lorebook_entry,
    get_workflow_attachment_by_id,
    get_world,
)
from ..database.models import ConversationRow
from ..inference import AbortToken
from ..workflows import WorkflowEventStream, public_event_error

logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "frontend")


# Per-root_id serialization for mutations of workflow_attachments groups.
# Regenerate, reroll-gen, and activate all write the root's
# active_sibling_id. BEGIN IMMEDIATE prevents data corruption, but commit
# order across concurrent transactions is indeterminate; the loser's API
# response can name a sibling whose active-pointer status the winner has
# already overwritten. The lock turns concurrent requests into sequential
# ones so the loser proceeds against post-winner state.
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


@asynccontextmanager
async def locked_attachment_group(aid: int, expected_message_id: int) -> AsyncIterator[tuple[Mapping[str, Any], int]]:
    """Hold the group-root lock for attachment ``aid``, stable against root promotion.

    The group root id (``parent_attachment_id or id``) is a *mutable* identity:
    deleting a root variant promotes the oldest surviving sibling to a new root
    (see ``delete_workflow_attachments``). Resolving the root from a pre-lock
    snapshot and then locking it has a time-of-check/time-of-use gap -- a
    concurrent delete can promote the root between the read and the acquire,
    leaving the caller holding an obsolete lock and mutating the group under a
    stale identity (two callers on the same logical group could even hold
    different keys, defeating serialization).

    This resolves the root, acquires its lock, then RE-READS ``aid`` under the
    lock. If the canonical root moved while acquiring, the held lock is stale, so
    it releases and retries on the new root. On success it yields the attachment
    snapshot read *under the held lock* together with the canonical root id, so a
    caller feeds its hook / insert a snapshot consistent with the lock it holds.

    Raises ``HTTPException`` 404 if the target does not exist or is not attached
    to ``expected_message_id`` (raised here, in the API layer, exactly as
    ``require_conversation`` does, so callers need no error mapping).
    ``BEGIN IMMEDIATE`` in the cache layer remains the final integrity boundary;
    this only stabilizes the process-local lock identity so same-group mutations
    serialize and a generative hook never runs against a since-deleted parent.
    Retry is unbounded, which is safe here: promotion requires this same lock, so
    churn cannot outrun acquisition on a single-user local app.
    """
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
    """Try-acquire the conversation stream lock without ever queuing.

    Yields ``True`` holding the lock when no pipeline stream is running on
    *cid*, ``False`` without it when one is. locked()/acquire() are atomic
    across coroutines (no await between — see the note in ``_sse_stream``), so
    the check cannot lose the lock to a stream and then block behind it. Lets
    read paths do stream-consistent side writes (greeting re-rolls) that must
    never wait out a running stream and must never interleave with its
    prompt-building reads.
    """
    lock = _conversation_stream_locks.setdefault(cid, asyncio.Lock())
    if lock.locked():
        yield False
        return
    await lock.acquire()
    try:
        yield True
    finally:
        lock.release()


# Per-conversation abort token for the active LLM generation. Set when streaming
# starts; cleared when it ends or is aborted. One token covers every client in
# the turn (writer + optional agent), so /stop signals them all at once.
_active_aborts: dict[str, AbortToken] = {}


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
# browser. An idle-timeout proxy in front of Orb (nginx proxy_read_timeout
# defaults to 60s) tears down such a silent SSE, which strands the still-running
# backend and drops the frontend to a stale draft. 15s stays comfortably under
# common proxy timeouts.
_SSE_KEEPALIVE_SECS = 15


async def _sse_stream(
    gen,
    request: Request,
    *,
    abort_token: AbortToken | None = None,
    cid: str | None = None,
):
    """Wrap an event-dict async generator as SSE, stopping cleanly on client disconnect.

    The primary stop path is the explicit POST /stop endpoint, which signals
    *abort_token*. That breaks out of the asyncio.wait() loop in complete() and
    lets the async-with block close the TCP connection to the LLM server
    normally — no task cancellation needed.

    A background watcher also polls request.is_disconnected() as a fallback
    for cases like the user closing the browser tab without clicking Stop.

    During long token-free stretches (director/editor thinking silently) a
    ``: keepalive`` SSE comment is emitted every ``_SSE_KEEPALIVE_SECS`` so an
    idle-timeout proxy can't drop the connection mid-turn. The comment carries no
    event/data line, so the frontend parser ignores it.
    """

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
    watcher: asyncio.Task | None = None
    try:
        if cid is not None:
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
            # clobbers the winner's entry. `lock is not None` in the finally
            # gates the matching pop to the same winner.
            if abort_token is not None:
                _active_aborts[cid] = abort_token
        watcher = asyncio.create_task(_watch_disconnect())
        gen_iter = gen.__aiter__()
        while True:
            nxt = asyncio.ensure_future(gen_iter.__anext__())
            try:
                # Race the next event against the keepalive interval: a silent
                # gap emits a comment frame and keeps waiting on the same task.
                while True:
                    done_set, _ = await asyncio.wait({nxt}, timeout=_SSE_KEEPALIVE_SECS)
                    if nxt in done_set:
                        break
                    yield ": keepalive\n\n"
            except BaseException:
                nxt.cancel()
                raise
            try:
                event = nxt.result()
            except StopAsyncIteration:
                break
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
        if cid and lock is not None:
            # `lock is not None` implies this coroutine won the acquire race and
            # therefore owns the _active_aborts entry it registered above.
            _active_aborts.pop(cid, None)
        if lock is not None:
            # Release before gen.aclose() so a queued /edit, /delete, or
            # /switch-branch can proceed in parallel with the inner generator's
            # cleanup rather than waiting on it.
            lock.release()
        await _safe_aclose(gen)


async def _encode_workflow_event_stream(events: AsyncIterator[dict]) -> AsyncGenerator[str, None]:
    """Serialize a workflow ``WorkflowEventStream`` as SSE frames -- API-owned wire encoding.

    Each event is validated against the shared public-event contract
    (:func:`public_event_error`); a malformed event is logged and dropped rather
    than corrupting the frame stream. The ``finally`` closes the underlying
    domain iterator, so on normal completion *and* on client disconnect (the
    wrapping :class:`_CleanupStreamingResponse` calls ``aclose`` on this
    generator) the workflow's own teardown -- e.g. cancelling an in-flight
    external render in its generator ``finally`` -- always runs.
    """
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
                nxt.cancel()
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


def _workflow_event_stream_response(stream: WorkflowEventStream) -> _CleanupStreamingResponse:
    """API-owned SSE response for a workflow ``WorkflowEventStream`` domain result.

    The workflow hook returns *what* to emit (validated event dicts); the API
    layer owns *how* it reaches the client. ``_CleanupStreamingResponse``
    guarantees the domain iterator is closed on client disconnect so the
    workflow can cancel in-flight work.
    """
    return _CleanupStreamingResponse(
        _encode_workflow_event_stream(stream.events),
        media_type="text/event-stream",
    )


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


# ── Shared Depends providers ─────────────────────────────────────────────────


async def require_conversation(cid: str) -> ConversationRow:
    """404 guard shared by the ``/api/conversations/{cid}/...`` routes."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conv


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


# A V3 entry may open with decorator lines (`@@depth 4`, `@@@fallback`, …).
_DECORATOR_PREAMBLE = re.compile(r"\A\s*(?:@@[^\n]*\n?)+")


def _strip_decorators(content: str) -> str:
    """Drop the V3 decorator preamble from an entry's content.

    ponytail: decorators are dropped, not interpreted — Orb's lorebook engine has
    no depth/role/position axis to map them onto, and the spec says an
    unrecognised decorator SHOULD be ignored. They must still be removed, or
    `@@depth 4` leaks into the prompt as literal text.
    """
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
    case_sensitive = item.get("caseSensitive") or item.get("case_sensitive")
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
        "use_regex": bool(item.get("use_regex", False)),
        # Cards in the wild set `selective` on every entry while leaving
        # secondary_keys empty; honouring that literally would make the whole
        # book match nothing, so an unbacked flag stores as false.
        "selective": bool(item.get("selective")) and bool(secondary_keys),
        "secondary_keys": secondary_keys,
    }


def lorebook_to_book(world_name: str, entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Serialise lorebook entry rows as a Tavern ``character_book`` dict.

    Export counterpart of :func:`_normalise_lorebook_entry` — keep the two
    field mappings in sync. The V3-only keys are additive, and readers that
    ignore unknown entry fields (Orb's own parser included) still see a valid
    V2 book.
    """
    return {
        "name": world_name,
        "extensions": {},
        "entries": [
            {
                "keys": e["keywords"],
                "content": e["content"],
                "extensions": {},
                "enabled": bool(e["enabled"]),
                "insertion_order": e["sort_order"],
                "case_sensitive": not bool(e["case_insensitive"]),
                "constant": bool(e.get("constant", False)),
                "name": e["name"],
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
