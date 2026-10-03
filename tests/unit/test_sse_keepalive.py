"""sse_stream emits keepalive comments during silent gaps, and settles a stopped stream.

A turn has long token-free stretches (reasoning-off director, the text-mode
editor prefill loop). Without a heartbeat an idle-timeout proxy drops the SSE
connection and strands the still-running backend. The comment frame must carry
no event/data line so the frontend parser ignores it, and real events must still
pass through untouched.
"""

from __future__ import annotations

import asyncio
import contextlib

import backend.api.deps as deps
from backend.inference import AbortToken


class _FakeReq:
    async def is_disconnected(self) -> bool:
        return False


async def test_keepalive_during_silence_and_events_passthrough(monkeypatch):
    monkeypatch.setattr(deps, "_SSE_KEEPALIVE_SECS", 0.05)

    async def gen():
        yield {"event": "token", "data": "hi"}
        await asyncio.sleep(0.17)  # silent gap → expect keepalives
        yield {"event": "done"}

    frames = [frame async for frame in deps.sse_stream(gen(), _FakeReq())]

    assert frames.count(": keepalive\n\n") >= 2
    assert "event: token\ndata: hi\n\n" in frames
    assert "event: done\ndata: \n\n" in frames
    # No keepalive is ever a well-formed event frame (frontend must ignore it).
    assert all(not f.startswith("event:") for f in frames if f == ": keepalive\n\n")


# ── Stop settlement ──────────────────────────────────────────────────────────
# /stop answers only once the stopped stream has finished saving, and the
# conversation lock is held until then, whether the client stays connected or
# goes away. The generators below stand in for a turn: they wind down when the
# token fires and then "save" behind a barrier the test controls.


class _Req:
    def __init__(self, disconnected: bool = False) -> None:
        self.disconnected = disconnected

    async def is_disconnected(self) -> bool:
        return self.disconnected


def _turn(token: AbortToken, saving: asyncio.Event, release: asyncio.Event, log: list[str]):
    async def gen():
        log.append("started")
        yield {"event": "token", "data": "partial"}
        await token.wait()
        # Winding down after the stop: the save still has to happen.
        saving.set()
        await release.wait()
        log.append("saved")
        yield {"event": "done"}

    return gen()


async def _wait_for(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 2)


async def test_stop_answers_after_the_stream_has_saved_and_released_the_lock():
    cid = "stop-settles"
    token, saving, release, log = AbortToken(), asyncio.Event(), asyncio.Event(), []
    frames: list[str] = []

    async def consume():
        async for frame in deps.sse_stream(_turn(token, saving, release, log), _Req(), abort_token=token, cid=cid):
            frames.append(frame)

    reader = asyncio.create_task(consume())
    while not frames:
        await asyncio.sleep(0)
    stop = asyncio.create_task(deps.stop_active_stream(cid))
    await _wait_for(saving)
    await asyncio.sleep(0.05)
    assert not stop.done(), "/stop answered while the reply was still saving"
    assert deps._conversation_stream_locks[cid].locked()

    release.set()
    assert await asyncio.wait_for(stop, 2) == {"active": True, "settled": True}
    await reader
    assert log == ["started", "saved"]
    assert "event: done\ndata: \n\n" in frames
    assert not deps._conversation_stream_locks[cid].locked()
    assert cid not in deps._active_streams
    # Nothing is registered any more; a later /stop does not wait.
    assert await deps.stop_active_stream(cid) == {"active": False, "settled": True}


async def test_a_disconnect_lets_the_turn_finish_saving_before_the_lock_is_released():
    """The request is cancelled mid-stream (the client went away). The turn is
    stopped, not cancelled: it runs to its save, and only then is the
    conversation free for the next request."""
    cid = "disconnect-drains"
    token, saving, release, log = AbortToken(), asyncio.Event(), asyncio.Event(), []
    first = asyncio.Event()

    async def consume():
        async for _ in deps.sse_stream(_turn(token, saving, release, log), _Req(), abort_token=token, cid=cid):
            first.set()

    reader = asyncio.create_task(consume())
    await _wait_for(first)
    reader.cancel()

    await _wait_for(saving)
    assert token.is_aborted
    assert deps._conversation_stream_locks[cid].locked(), "lock released while the turn was still saving"
    stop = asyncio.create_task(deps.stop_active_stream(cid))
    await asyncio.sleep(0.05)
    assert not stop.done()

    release.set()
    assert await asyncio.wait_for(stop, 2) == {"active": True, "settled": True}
    with contextlib.suppress(asyncio.CancelledError):
        await reader
    assert log == ["started", "saved"]
    assert not deps._conversation_stream_locks[cid].locked()


async def test_a_request_whose_client_left_before_registration_never_generates():
    """A Stop that reached the server before the request did: the browser has
    already dropped the request, so the turn starts stopped."""
    token = AbortToken()
    seen: list[bool] = []

    async def gen():
        seen.append(token.is_aborted)
        yield {"event": "done"}

    frames = [frame async for frame in deps.sse_stream(gen(), _Req(disconnected=True), abort_token=token, cid="early")]

    assert seen == [True]
    assert frames == ["event: done\ndata: \n\n"]
