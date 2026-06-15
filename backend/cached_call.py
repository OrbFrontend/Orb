"""
cached_call.py — The core cached-call execution path shared by every pass.

Defines the single completion chokepoint (:func:`cached_complete`) and the
byte-identical prompt base every pass extends (:class:`CachedBase`). The
orchestrator and all four passes funnel their LLM calls through here so the
cache-relevant bytes are computed in exactly one place.

This is the *core* path: it depends on the debug KV-cache tracker
(``kv_tracker._KVCacheTracker``) only as an optional object passed in by callers,
never structurally — the reference is type-only (``TYPE_CHECKING``), so the core
cached-call path has no runtime dependency on the debug tooling.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from .kv_tracker import _KVCacheTracker


async def cached_complete(
    client: Any,
    *,
    label: str,
    messages: Sequence[Mapping[str, Any]],
    model: str,
    tools: list[dict] | None = None,
    tool_choice: "dict | str | None" = None,
    kv_tracker: "_KVCacheTracker | None" = None,
    record: bool = True,
    **params: Any,
) -> AsyncIterator[dict]:
    """Run ``client.complete`` and snapshot the KV-cache view from the *same*
    arguments it is called with, so the tracker can never drift from what was
    actually sent to the model.

    This is the single chokepoint every pipeline pass funnels its completions
    through. ``record()`` (the prompt snapshot) and ``record_usage()`` (provider
    truth) are bound to the one ``client.complete`` call here, eliminating the
    old failure mode where a pass recorded one ``messages``/``tools`` blob and
    then sent a different one.

    ``record=True`` (default) snapshots the prompt before issuing the call, so
    each call appends one tracker entry. A multi-call loop (the editor's ReAct
    iterations) therefore shows *every* iteration — surfacing any mid-loop change
    to the tools blob that would otherwise be invisible. Provider ``usage`` from
    the terminal ``done`` event is attached to the latest entry for *label*. All
    events from ``client.complete`` are yielded through unchanged.
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


@dataclass(frozen=True)
class CachedBase:
    """The byte-identical bottom of the prompt stack for one turn on one
    inference server: the system+history *prefix*, the *tools* blob, and the
    *model*. Built once per server per turn and shared by every pass that runs
    on that server, so the cache-relevant bytes are computed in exactly one
    place and can never be reconstructed — and so silently diverge — per pass.

    Passes EXTEND this base via :meth:`complete`; they never rebuild it. The
    fields are frozen and stored as tuples so the shared instance cannot have
    its prefix or tool list mutated, reordered, or swapped out mid-turn — the
    failure mode the invariants in docs/architecture/kv-cache.md and
    tests/unit/test_kv_cache_invariants.py exist to catch.

    In dual-model turns there are two bases: one for the writer's server and one
    for the agent (director + editor) server. Invariant 5 — "the writer drops
    tools when it runs on a different server than the agent" — is then just a
    property of how the writer's base is built (empty ``tools``), not a flag
    threaded through the writer pass.

    ``resolve`` is the last step of turning the assembled stack into the literal
    bytes on the wire: an opaque ``messages -> messages`` transform applied to
    ``[*prefix, *trailing]`` immediately before the call (in practice
    ``Macros.resolve_prompt_messages``, scrubbing ``{{user}}``/``{{char}}`` from
    whatever a pass appended). Keeping it on the base means the tracker snapshot
    is taken from the *resolved* bytes — the same ones sent — so it cannot drift.
    ``None`` means send the assembled stack unchanged.
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
        tool_choice: "dict | str | None" = None,
        kv_tracker: "_KVCacheTracker | None" = None,
        record: bool = True,
        **params: Any,
    ) -> AsyncIterator[dict]:
        """Issue one completion that extends this base with *trailing* (the
        per-pass top of the stack). The cached bottom — prefix + tools + model —
        comes solely from ``self``; only *trailing* and *tool_choice* vary.

        The assembled stack is run through ``self.resolve`` (if set) to produce
        the final wire bytes, then handed to :func:`cached_complete` so the
        tracker snapshot is taken from the exact bytes sent.
        """
        messages: Sequence[Mapping[str, Any]] = [*self.prefix, *trailing]
        if self.resolve is not None:
            messages = self.resolve(messages)
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
