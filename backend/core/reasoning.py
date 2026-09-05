"""Reasoning-buffer policy: one blank line between calls."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any

__all__ = [
    "CALL_BREAK",
    "ReasoningChannel",
    "joined_delta",
    "mark_call_start",
    "reasoning_delta_event",
]

# Two newlines render as one blank line in the reasoning box.
CALL_BREAK = "\n\n"


async def mark_call_start(events: AsyncIterator[dict]) -> AsyncIterator[dict]:
    """Mark the first reasoning delta in one completion stream."""
    marked = False
    async for event in events:
        if not marked and event.get("type") == "reasoning":
            marked = True
            event = {**event, "call_start": True}
        yield event


def reasoning_delta_event(event: Mapping[str, Any]) -> dict:
    """Copy a reasoning event while preserving its call-boundary marker."""
    out: dict = {"type": "reasoning", "delta": event.get("delta", "")}
    if event.get("call_start"):
        out["call_start"] = True
    return out


def joined_delta(buffered: str, event: Mapping[str, Any]) -> str:
    """Return the delta, adding the missing part of a call-separating blank line."""
    delta = event.get("delta", "")
    if not event.get("call_start") or not buffered:
        return delta
    have = (len(buffered) - len(buffered.rstrip("\n"))) + (len(delta) - len(delta.lstrip("\n")))
    missing = len(CALL_BREAK) - have
    return "\n" * missing + delta if missing > 0 else delta


class ReasoningChannel:
    """In-memory reasoning buffer for workflow calls."""

    __slots__ = ("text",)

    def __init__(self, text: str = "") -> None:
        self.text = text

    def push(self, event: Mapping[str, Any]) -> str:
        """Append one event and return the exact delta to stream."""
        delta = joined_delta(self.text, event)
        self.text += delta
        return delta
