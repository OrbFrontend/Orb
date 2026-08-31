"""Shared completion entry point and cached prompt base."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .kv_tracker import _KVCacheTracker

logger = logging.getLogger(__name__)


def _render_tail(tail: Sequence[Mapping[str, Any]]) -> str:
    """Flatten the per-call tail messages for the console log.

    Only the tail is logged: the prefix is byte-identical across every pass of
    the turn, so printing it once per call is noise. Multimodal parts render as
    their text, non-text parts as a ``[type]`` marker — never the base64 blob.
    """
    out = []
    for m in tail:
        content = m.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text") or f"[{p.get('type', 'part')}]" for p in content)
        out.append(f"--- {m.get('role')} ---\n{content}")
    return "\n".join(out)


async def cached_complete(
    client: Any,
    *,
    label: str,
    messages: Sequence[Mapping[str, Any]],
    model: str,
    tools: list[dict] | None = None,
    tool_choice: dict | str | None = None,
    kv_tracker: _KVCacheTracker | None = None,
    record: bool = True,
    **params: Any,
) -> AsyncIterator[dict]:
    """Run ``client.complete`` and snapshot the KV tracker from the same args.

    Every pass funnels through here so the tracker always sees exactly what
    was sent. ``record=True`` (default) snapshots before the call; each
    iteration of a multi-call loop (e.g. the editor's ReAct loop) adds its own
    entry. Provider usage from the terminal ``done`` event is attached to the
    latest entry for *label*. All events are yielded through unchanged.
    """
    if kv_tracker is not None and record:
        kv_tracker.record(label, messages, tools, model=model)
    async for event in client.complete(
        messages=messages,
        model=model,
        tools=tools,
        tool_choice=tool_choice,
        **params,
    ):
        if event["type"] == "done" and kv_tracker is not None:
            kv_tracker.record_usage(label, event.get("usage"))
        yield event


async def _relay_reasoning(stream: AsyncIterator[dict], reply: dict) -> AsyncIterator[dict]:
    """Forward *stream*'s reasoning deltas; collect its ``done`` message in *reply*."""
    async for event in stream:
        if event["type"] == "reasoning":
            yield {"type": "reasoning", "delta": event["delta"]}
        elif event["type"] == "done":
            reply.update(event["message"])


@dataclass(frozen=True)
class CachedBase:
    """Frozen prompt base shared by calls on one server."""

    prefix: tuple[Mapping[str, Any], ...]
    tools: tuple[dict, ...]
    model: str
    resolve: Callable[[Sequence[Mapping[str, Any]]], list[dict]] | None = None

    def complete(
        self,
        client: Any,
        *,
        label: str,
        trailing: Sequence[Mapping[str, Any]],
        tool_choice: dict | str | None = None,
        kv_tracker: _KVCacheTracker | None = None,
        record: bool = True,
        **params: Any,
    ) -> AsyncIterator[dict]:
        """Issue one completion extending this base with *trailing*.

        The cached bottom (prefix + tools + model) comes from ``self``; only
        *trailing* and *tool_choice* vary per call. The stack is resolved via
        ``self.resolve`` if set, then handed to :func:`cached_complete`.
        """
        messages: Sequence[Mapping[str, Any]] = [*self.prefix, *trailing]
        if self.resolve is not None:
            messages = self.resolve(messages)
        logger.info("Pass %s tail:\n%s", label, _render_tail(messages[len(self.prefix) :]))
        return cached_complete(
            client,
            label=label,
            messages=messages,
            model=self.model,
            tools=list(self.tools) or None,
            tool_choice=tool_choice,
            kv_tracker=kv_tracker,
            record=record,
            **params,
        )

    def complete_into(self, client: Any, reply: dict, **kw: Any) -> AsyncIterator[dict]:
        """:meth:`complete`, demuxed the way every agentic pass consumes it.

        Yields only the reasoning deltas — for the pass to forward onto its own
        event stream — and collects the terminal assembled message into *reply*.
        *reply* is filled in place rather than returned because an async
        generator cannot return a value; it stays ``{}`` when the call produced
        no message, which is the "model skipped" shape the passes already
        handle.
        """
        return _relay_reasoning(self.complete(client, **kw), reply)
