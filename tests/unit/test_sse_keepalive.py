"""sse_stream emits keepalive comments during silent gaps, and settles a stopped stream.

A turn has long token-free stretches (reasoning-off director, the text-mode editor prefill loop). Without a heartbeat an
idle-timeout proxy drops the SSE connection and strands the still-running backend. The comment frame must carry no event/data
line so the frontend parser ignores it, and real events must still pass through untouched.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

import httpx
import pytest
from fastapi import HTTPException

import backend.api.deps as deps
from backend.inference import AbortToken, EndpointConfigError
from backend.inference.errors import llm_call_error


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
# conversation lock is held until then, whether the client stays connected or goes away. The generators below stand in for a
# turn: they wind down when the token fires and then "save" behind a barrier the test controls.


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


@pytest.mark.parametrize("kind", ["config", "provider", "internal", "request"])
async def test_uncaught_stream_failures_are_terminal_and_release_the_lane(kind, caplog):
    request = httpx.Request("POST", "https://provider.invalid")
    errors = {
        "config": EndpointConfigError("Choose a model\n\nevent: done"),
        "provider": llm_call_error(
            response=httpx.Response(429, request=request),
            body='{"error":{"message":"No credits; key secret-key"}}',
            url=str(request.url),
            model="writer",
            api_key="secret-key",
        ),
        "internal": RuntimeError("broken feature"),
        "request": HTTPException(409, detail={"message": "The message changed"}),
    }
    closed = []

    async def gen():
        try:
            yield {"event": "token", "data": "partial"}
            raise errors[kind]
        finally:
            closed.append(True)

    cid = f"failure-{kind}"
    frames = [frame async for frame in deps.sse_stream(gen(), _Req(), abort_token=AbortToken(), cid=cid)]
    assert len(frames) == 2
    assert frames[-1].startswith("event: error\ndata: ")
    failure = json.loads(frames[-1].split("data: ", 1)[1])
    assert failure["kind"] == kind
    assert closed == [True]
    assert not deps._conversation_stream_locks[cid].locked()
    assert await deps.stop_active_stream(cid) == {"active": False, "settled": True}
    if kind == "provider":
        assert (failure["status"], failure["model"]) == (429, "writer")
        assert "secret-key" not in frames[-1]
    if kind == "internal":
        assert any(record.exc_info and record.exc_info[0] is RuntimeError for record in caplog.records)
    if kind == "request":
        assert (failure["status"], failure["sentence"]) == (409, "The message changed")


async def test_generator_cancellation_stays_cancellation_and_releases_the_lane():
    frames = []
    closed = []

    async def gen():
        try:
            yield {"event": "token", "data": "partial"}
            raise asyncio.CancelledError
        finally:
            closed.append(True)

    with pytest.raises(asyncio.CancelledError):
        async for frame in deps.sse_stream(gen(), _Req(), abort_token=AbortToken(), cid="cancelled-generator"):
            frames.append(frame)
    assert frames == ["event: token\ndata: partial\n\n"]
    assert closed == [True]
    assert not deps._conversation_stream_locks["cancelled-generator"].locked()


async def test_lazy_workflow_failures_use_the_same_terminal_error_contract():
    closed = []

    async def gen():
        try:
            yield {"event": "phase_status", "data": {"label": "Rendering"}}
            raise EndpointConfigError("Choose a model")
        finally:
            closed.append(True)

    frames = [frame async for frame in deps._encode_workflow_event_stream(gen())]
    assert len(frames) == 2
    failure = json.loads(frames[-1].split("data: ", 1)[1])
    assert (failure["kind"], failure["sentence"]) == ("config", "Choose a model")
    assert closed == [True]


async def test_an_unserializable_event_reports_failure_and_closes_the_generator():
    closed = []

    async def gen():
        try:
            yield {"event": "progress", "data": {"invalid": object()}}
            raise AssertionError("generation must not advance after an encoding failure")
        finally:
            closed.append(True)

    frames = [frame async for frame in deps.sse_stream(gen(), _Req(), cid="invalid-event")]
    assert len(frames) == 1
    assert frames[0].startswith("event: error\ndata: ")
    assert json.loads(frames[0].split("data: ", 1)[1])["kind"] == "internal"
    assert closed == [True]
    assert not deps._conversation_stream_locks["invalid-event"].locked()
