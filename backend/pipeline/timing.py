"""Non-overlapping generation step timings for the Inspector."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, TypeVar

from ..database.models import GenerationTimingRow
from .events import LatencyEvent

if TYPE_CHECKING:
    from .state import TurnState

Event = TypeVar("Event", bound=Mapping[str, Any])


@contextmanager
def measure_step(
    entries: list[GenerationTimingRow] | None,
    phase: str,
    label: str,
    *,
    fragment_id: str = "",
    stopped: Callable[[], bool] | None = None,
) -> Iterator[GenerationTimingRow]:
    """Keep elapsed time even when a step fails or is interrupted."""
    entry: GenerationTimingRow = {"phase": phase, "label": label, "elapsed_ms": 0, "status": "completed"}
    if fragment_id:
        entry["fragment_id"] = fragment_id
    started = time.monotonic()
    try:
        yield entry
    except (asyncio.CancelledError, GeneratorExit):
        entry["status"] = "stopped"
        raise
    except BaseException:
        entry["status"] = "failed"
        raise
    finally:
        entry["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
        if stopped is not None and stopped():
            entry["status"] = "stopped"
        if entries is not None:
            entries.append(entry)


async def timed_events(
    events: AsyncIterator[Event],
    entries: list[GenerationTimingRow] | None,
    phase: str,
    label: str,
    *,
    stopped: Callable[[], bool] | None = None,
    fragment_id: str = "",
) -> AsyncGenerator[Event, None]:
    """Relay events unchanged and record the step, including handled failures."""
    with measure_step(entries, phase, label, stopped=stopped, fragment_id=fragment_id) as entry:
        iterator = aiter(events)
        try:
            async for event in iterator:
                if event.get("type") == "failure" or event.get("event") == "warning":
                    entry["status"] = "failed"
                yield event
        finally:
            # A wrapper closed while suspended at yield must finish its source's cleanup
            # before the timer settles and persistence reads the result.
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()


def latency_event(state: TurnState) -> LatencyEvent:
    """Update the total and publish a snapshot with independent rows."""
    if state.latency_breakdown:
        state.latency = sum(int(entry["elapsed_ms"]) for entry in state.latency_breakdown)
    return {
        "event": "latency",
        "data": {
            "agent_latency_ms": state.latency,
            "latency_breakdown": [entry.copy() for entry in state.latency_breakdown],
        },
    }
