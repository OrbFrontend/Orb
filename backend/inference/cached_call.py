"""
cached_call.py — Shared completion chokepoint for every pipeline pass.

Defines :func:`cached_complete` (the single call site all passes funnel
through) and :class:`CachedBase` (the shared prefix + tools + model bottom
of the prompt stack). The KV tracker is an optional pass-in; this module has
no runtime dependency on it.
"""

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
        # Transport-inert. The pass name is already the tracker's key; passing
        # it through means a client (or a test double) can tell the Editor's
        # ``tool_choice="auto"`` from the Writer's, which are the same wire
        # value with different meanings.
        label=label,
        **params,
    ):
        if event["type"] == "done" and kv_tracker is not None:
            kv_tracker.record_usage(label, event.get("usage"))
        yield event


@dataclass(frozen=True)
class CachedBase:
    """The shared bottom of the prompt stack for one turn on one server.

    Holds the system+history *prefix*, the *tools* blob, and the *model*.
    Built once per server per turn; all passes on that server extend it via
    :meth:`complete` rather than rebuilding it. Fields are frozen tuples so
    nothing can mutate or reorder the shared base mid-turn.

    In dual-model turns there are two bases — one for the writer's server, one
    for the agent (director + editor) server. The writer's base simply has an
    empty ``tools`` tuple, which is how Invariant 5 is enforced without
    threading a flag through the writer pass.

    ``resolve`` is an optional ``messages -> messages`` transform applied to
    ``[*prefix, *trailing]`` right before the call (in practice
    ``Macros.resolve_prompt_messages``, which scrubs ``{{user}}``/``{{char}}``
    from pass-appended content). The tracker snapshot is taken after resolution,
    so it always matches what was actually sent. ``None`` means no transform.
    """

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
