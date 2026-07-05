"""_sse_stream emits keepalive comments during silent gaps.

A turn has long token-free stretches (reasoning-off director, the text-mode
editor prefill loop). Without a heartbeat an idle-timeout proxy drops the SSE
connection and strands the still-running backend. The comment frame must carry
no event/data line so the frontend parser ignores it, and real events must still
pass through untouched.
"""

from __future__ import annotations

import asyncio

import backend.api.deps as deps


class _FakeReq:
    async def is_disconnected(self) -> bool:
        return False


async def test_keepalive_during_silence_and_events_passthrough(monkeypatch):
    monkeypatch.setattr(deps, "_SSE_KEEPALIVE_SECS", 0.05)

    async def gen():
        yield {"event": "token", "data": "hi"}
        await asyncio.sleep(0.17)  # silent gap → expect keepalives
        yield {"event": "done"}

    frames = [frame async for frame in deps._sse_stream(gen(), _FakeReq())]

    assert frames.count(": keepalive\n\n") >= 2
    assert "event: token\ndata: hi\n\n" in frames
    assert "event: done\ndata: \n\n" in frames
    # No keepalive is ever a well-formed event frame (frontend must ignore it).
    assert all(not f.startswith("event:") for f in frames if f == ": keepalive\n\n")
