"""Generation timing survives failures and Stop and snapshots never double-count."""

import asyncio
from types import SimpleNamespace

import pytest

from backend.pipeline import timing
from backend.pipeline.state import TurnState


async def test_timings_keep_failed_and_stopped_work_and_publish_independent_snapshots(monkeypatch):
    clock = iter([10.0, 10.25, 20.0, 20.75, 30.0, 30.5])
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    state = TurnState()

    async def events(*, failure=False):
        yield {"type": "reasoning", "delta": "kept"}
        if failure:
            raise RuntimeError("endpoint failed")

    with pytest.raises(RuntimeError, match="endpoint failed"):
        async for _ in timing.timed_events(events(failure=True), state.latency_breakdown, "director", "Pacing"):
            pass
    first = timing.latency_event(state)
    assert first["data"]["agent_latency_ms"] == 250
    assert first["data"]["latency_breakdown"][0]["status"] == "failed"

    async for _ in timing.timed_events(events(), state.latency_breakdown, "writer", "Writer", stopped=lambda: True):
        pass
    # A no gate still consumed time. It contributes once and stays identified as skipped.
    with timing.measure_step(state.latency_breakdown, "editor", "Trim", fragment_id="trim") as entry:
        entry["status"] = "skipped"
    final = timing.latency_event(state)
    assert final["data"]["agent_latency_ms"] == 1500
    assert [row["status"] for row in final["data"]["latency_breakdown"]] == ["failed", "stopped", "skipped"]
    assert len(first["data"]["latency_breakdown"]) == 1
    assert timing.latency_event(state) == final
    restored = TurnState(**state.as_result_event_data())
    assert timing.latency_event(restored) == final


async def test_cancellation_records_elapsed_time_before_fallback_persistence(monkeypatch):
    clock = iter([1.0, 1.5])
    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=lambda: next(clock)))
    state = TurnState()

    async def cancelled():
        yield {"type": "content", "delta": "partial draft"}
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        async for event in timing.timed_events(cancelled(), state.latency_breakdown, "writer", "Writer"):
            state.resp_text += event["delta"]
    assert state.resp_text == "partial draft"
    assert timing.latency_event(state)["data"]["agent_latency_ms"] == 500
    assert state.latency_breakdown[0]["status"] == "stopped"


async def test_closing_a_suspended_timer_closes_its_source_before_recording(monkeypatch):
    closed = False

    def monotonic():
        return 2.5 if closed else 2.0

    monkeypatch.setattr(timing, "time", SimpleNamespace(monotonic=monotonic))
    state = TurnState()

    async def source():
        nonlocal closed
        try:
            yield {"type": "reasoning", "delta": "partial thought"}
        finally:
            await asyncio.sleep(0)
            closed = True

    events = timing.timed_events(source(), state.latency_breakdown, "director", "Pacing")
    assert (await anext(events))["delta"] == "partial thought"
    await events.aclose()
    assert closed, "the upstream generator must not outlive the timing wrapper"
    assert state.latency_breakdown == [{"phase": "director", "label": "Pacing", "elapsed_ms": 500, "status": "stopped"}]
