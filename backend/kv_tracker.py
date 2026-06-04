"""
kv_tracker.py — KV-cache hit/miss tracker shared across passes.

Reports two views per LLM call:

  1. Provider — ground truth. The ``usage`` field from each response is parsed
     with fallbacks across OpenAI / Anthropic / DeepSeek / vLLM naming and
     printed as ``cached/total`` tokens. This is the only number that
     reconciles with your provider's billing dashboard.

  2. Local estimate — a debugging aid, NOT a prediction of the provider
     number. We track two things separately:
       - ``msgs_overlap``: char-prefix overlap of the messages list serialized
         alone.  Captures whether system prompt + history + most of the final
         user message are shared with the previous same-model call.
       - ``tools_match``:  whether the tools list is byte-identical to the
         previous same-model call.

     We deliberately do NOT combine these into a single "estimated hit
     percentage." Where the chat template renders tools (start vs. end of the
     system block vs. before the final user turn) determines whether a tools
     diff actually breaks the wire-level cache, and the tracker has no way to
     know that. Two split numbers + provider truth lets a human read both
     failure modes:
       - msgs_overlap high, tools_match False → cache may or may not hold;
         provider number tells you which.
       - msgs_overlap low                     → cache is broken regardless.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Mapping, Sequence

logger = logging.getLogger(__name__)

# Per-conversation snapshot of the previous turn's entries, keyed by conversation_id.
_prev_turn_entries: dict[str, list[dict]] = {}


def _serialize_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """Compact JSON serialization of a messages list; sort_keys for byte-stable output regardless of dict construction order."""
    return "\n".join(json.dumps(m, separators=(",", ":"), sort_keys=True) for m in messages)


def _serialize_tools(tools: list[dict] | None) -> str:
    """Compact, order-deterministic JSON for tools. Empty string when no tools."""
    if not tools:
        return ""
    return json.dumps(tools, separators=(",", ":"), sort_keys=True)


def _common_prefix_len(a: str, b: str) -> int:
    i = 0
    limit = min(len(a), len(b))
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def extract_cache_stats(usage: dict | None) -> dict:
    """Pull cache hit / write / total token counts from a provider ``usage``
    dict, with fallbacks across naming conventions.

    Recognises:
      - OpenAI / vLLM / llama.cpp:  usage.prompt_tokens_details.cached_tokens
      - Anthropic:                  usage.cache_read_input_tokens,
                                    usage.cache_creation_input_tokens
      - DeepSeek:                   usage.prompt_cache_hit_tokens

    Returns ``prompt_tokens``, ``cached_tokens``, ``cache_write_tokens``, and
    ``source`` (which field path was used — useful when debugging why a
    provider's numbers look off). When ``usage`` is missing or unrecognized,
    counts are 0 and ``source`` distinguishes "missing", "unrecognized", and
    "no_cache_fields" so callers can tell "no data" from a real zero.
    """
    if not isinstance(usage, dict):
        return {
            "prompt_tokens": 0,
            "cached_tokens": 0,
            "cache_write_tokens": 0,
            "source": "missing",
        }

    prompt_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0

    cached = 0
    source = "unrecognized"

    details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details")
    if isinstance(details, dict):
        v = details.get("cached_tokens") or details.get("cache_read_tokens") or 0
        if v:
            cached, source = int(v), "prompt_tokens_details.cached_tokens"

    if not cached:
        v = usage.get("cache_read_input_tokens") or 0
        if v:
            cached, source = int(v), "cache_read_input_tokens"

    if not cached:
        v = usage.get("prompt_cache_hit_tokens") or 0
        if v:
            cached, source = int(v), "prompt_cache_hit_tokens"

    cache_write = int(usage.get("cache_creation_input_tokens") or 0)
    if cache_write and source == "unrecognized":
        source = "cache_creation_input_tokens"

    if int(prompt_tokens or 0) > 0 and source == "unrecognized" and not cache_write:
        source = "no_cache_fields"

    return {
        "prompt_tokens": int(prompt_tokens or 0),
        "cached_tokens": cached,
        "cache_write_tokens": cache_write,
        "source": source,
    }


class _KVCacheTracker:
    def __init__(self, conversation_id: str | None = None):
        self._entries: list[dict] = []
        self._conversation_id = conversation_id
        self._prev_entries: list[dict] = list(_prev_turn_entries.get(conversation_id, [])) if conversation_id else []

    def record(
        self,
        label: str,
        messages: Sequence[Mapping[str, Any]],
        tools: list[dict] | None,
        model: str = "",
    ) -> None:
        """Snapshot a single LLM call. Call once per pass (or per tool in director)."""
        msgs_serialized = _serialize_messages(messages)
        tools_serialized = _serialize_tools(tools)
        self._entries.append(
            {
                "label": label,
                "model": model,
                "msgs_serialized": msgs_serialized,
                "tools_serialized": tools_serialized,
                "msgs_chars": len(msgs_serialized),
                "tools_chars": len(tools_serialized),
                "tools_names": [t.get("function", {}).get("name", "") for t in (tools or [])],
                "usage": None,
            }
        )

    def record_usage(self, label: str, usage: dict | None) -> None:
        """Attach the provider's ``usage`` dict to the most recent entry with this label."""
        for entry in reversed(self._entries):
            if entry["label"] == label:
                entry["usage"] = usage
                return
        logger.debug("record_usage: no prior record() for label=%r, dropping", label)

    def _find_prev(self, i: int, model: str, label: str) -> tuple[dict | None, bool]:
        """Return (prev_entry, is_cross_turn). Matches same-model first within
        this turn, then falls back to same-(label, model) from the previous turn."""
        for j in range(i - 1, -1, -1):
            if self._entries[j].get("model", "") == model:
                return self._entries[j], False
        if self._prev_entries:
            for p in self._prev_entries:
                if p["label"] == label and p.get("model", "") == model:
                    return p, True
        return None, False

    def log_summary(self) -> None:
        if not self._entries:
            return

        lines = ["KV cache report  (provider = truth; local = msgs-prefix + tools-match, template-dependent):"]
        total_cached = 0
        total_prompt = 0

        for i, e in enumerate(self._entries):
            model = e.get("model", "")
            prev, cross_turn = self._find_prev(i, model, e["label"])

            # ── Local view: messages prefix + tools identity, reported separately
            if prev is None:
                local_note = "local: baseline"
            else:
                msgs_overlap = _common_prefix_len(prev["msgs_serialized"], e["msgs_serialized"])
                msgs_total = e["msgs_chars"]
                msgs_pct = (msgs_overlap / msgs_total * 100) if msgs_total else 0
                tools_match = e["tools_serialized"] == prev["tools_serialized"] and e["tools_chars"] > 0
                if e["tools_chars"] == 0 and prev["tools_chars"] == 0:
                    tools_note = "tools=none"
                elif tools_match:
                    tools_note = "tools_MATCH"
                else:
                    tools_note = f"tools_DIFFER (prev={prev['tools_chars']}c, this={e['tools_chars']}c)"
                turn_tag = "prev-turn " if cross_turn else ""
                local_note = (
                    f"local: msgs_overlap={msgs_overlap}/{msgs_total}c ({msgs_pct:.1f}%) "
                    f"vs {turn_tag}{prev['label']!r}; {tools_note}"
                )

            # ── Provider view: ground truth from usage
            stats = extract_cache_stats(e.get("usage"))
            if stats["source"] == "missing":
                provider_note = "provider: N/A (no usage returned)"
            elif stats["source"] in ("unrecognized", "no_cache_fields"):
                provider_note = f"provider: prompt={stats['prompt_tokens']} tok  cached=N/A [{stats['source']}]"
            else:
                pt, ct, cw = (
                    stats["prompt_tokens"],
                    stats["cached_tokens"],
                    stats["cache_write_tokens"],
                )
                total_cached += ct
                total_prompt += pt
                pct = (ct / pt * 100) if pt else 0.0
                write_part = f"  write={cw}" if cw else ""
                provider_note = f"provider: cached={ct}/{pt} tok ({pct:.1f}%){write_part} [{stats['source']}]"

            lines.append(f"  {e['label']:<28}  {provider_note}  |  {local_note}")

        if total_prompt:
            pct_total = total_cached / total_prompt * 100
            lines.append(f"  Totals — provider cached: {total_cached}/{total_prompt} tok ({pct_total:.1f}%)")
        else:
            lines.append("  Totals — provider cached: N/A (server returned no usage data)")

        logger.info("\n".join(lines))

        if self._conversation_id:
            _prev_turn_entries[self._conversation_id] = list(self._entries)


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
