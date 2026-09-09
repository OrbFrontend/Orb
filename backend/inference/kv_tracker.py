"""Track provider and local KV-cache signals for each turn."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import Any

logger = logging.getLogger(__name__)

# Per-conversation snapshot of the previous turn's entries, keyed by conversation_id.
_prev_turn_entries: dict[str, list[dict]] = {}


def _serialize_messages(messages: Sequence[Mapping[str, Any]]) -> str:
    """Compact, byte-stable JSON for a messages list (sorted keys)."""
    return "\n".join(json.dumps(m, separators=(",", ":"), sort_keys=True) for m in messages)


def _serialize_tools(tools: list[dict] | None) -> str:
    """Compact, order-deterministic JSON for a tools list. Empty string when ``tools`` is empty."""
    if not tools:
        return ""
    return json.dumps(tools, separators=(",", ":"), sort_keys=True)


def _lane_of(entry: Mapping[str, Any]) -> tuple[str, str, str]:
    """The physical cache owner and rendered-prompt shape for an entry."""
    return (
        entry.get("endpoint", "") or "",
        entry.get("model", "") or "",
        entry.get("shape", "") or "",
    )


def _lane_name(lane: tuple[str, str, str]) -> str:
    """Short, readable name for a lane, for the report's legend."""
    endpoint, model, shape = lane
    host = endpoint.split("://", 1)[-1].split("/", 1)[0]
    suffix = f" [{shape}]" if shape else ""
    return f"{model or '?'}@{host or 'local'}{suffix}"


def _common_prefix_len(a: str, b: str) -> int:
    i = 0
    limit = min(len(a), len(b))
    while i < limit and a[i] == b[i]:
        i += 1
    return i


def extract_cache_stats(usage: dict | None) -> dict:
    """Extract cache hit/write/total token counts from a provider ``usage`` dict.

    Recognises naming conventions across providers:
      - OpenAI / vLLM / llama.cpp: ``prompt_tokens_details.cached_tokens``
      - Anthropic: ``cache_read_input_tokens``, ``cache_creation_input_tokens``
      - DeepSeek: ``prompt_cache_hit_tokens``

    Returns ``prompt_tokens``, ``cached_tokens``, ``cache_write_tokens``, and
    ``source`` (the field path used — handy when debugging provider numbers).
    When ``usage`` is missing or unrecognised, counts are 0 and ``source`` is
    one of ``"missing"``, ``"unrecognized"``, or ``"no_cache_fields"``.
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
        # How far ``log_summary`` has already printed. One tracker can be summarised
        # several times per request — a group exchange runs one pipeline per speaker
        # and each one summarises on its way out — and reprinting the whole list
        # every time made the report grow quadratically with cast size, burying the
        # calls the reader opened the log for.
        self._reported = 0

    def record(
        self,
        label: str,
        messages: Sequence[Mapping[str, Any]],
        tools: list[dict] | None,
        model: str = "",
        endpoint: str = "",
        shape: str = "",
    ) -> None:
        """Snapshot one LLM call (messages + tools). Call once per pass or per director tool.

        *endpoint* and *model* identify the physical cache owner. *shape* is a
        stable discriminator for an intentionally separate rendered-prompt family
        on that owner, such as a self-contained workflow call. It is deliberately
        not derived from the current bytes: accidental changes within one family
        must stay comparable so the report can expose them.
        """
        msgs_serialized = _serialize_messages(messages)
        tools_serialized = _serialize_tools(tools)
        self._entries.append(
            {
                "label": label,
                "model": model,
                "endpoint": endpoint,
                "shape": shape,
                "msgs_serialized": msgs_serialized,
                "tools_serialized": tools_serialized,
                "msgs_chars": len(msgs_serialized),
                "tools_chars": len(tools_serialized),
                "tools_names": [t.get("function", {}).get("name", "") for t in (tools or [])],
                "usage": None,
            }
        )

    def record_usage(self, label: str, usage: dict | None) -> None:
        """Attach the provider ``usage`` dict to the most recent entry for *label*."""
        for entry in reversed(self._entries):
            if entry["label"] == label:
                entry["usage"] = usage
                return
        logger.debug("record_usage: no prior record() for label=%r, dropping", label)

    def _find_prev(self, i: int, lane: tuple[str, str, str], label: str) -> tuple[dict | None, bool]:
        """Find the previous cache entry on *lane* to compare against.

        A comparison only means something within one lane: across lanes the tools
        blob and the system prompt differ by design, so a cross-lane "diff" reads
        as a cache bust that no one can fix.
        """
        for j in range(i - 1, -1, -1):
            if _lane_of(self._entries[j]) == lane:
                return self._entries[j], False
        for p in reversed(self._prev_entries):
            if p["label"] == label and _lane_of(p) == lane:
                return p, True
        return None, False

    def log_summary(self) -> None:
        """Print the calls recorded since the last summary, comparisons unchanged.

        Only *unreported* entries are rendered; ``_find_prev`` still ranges over
        the whole list, so a group exchange's second speaker is still measured
        against the first speaker's calls — it just isn't reprinted alongside them.
        """
        first = self._reported
        if first >= len(self._entries):
            return
        self._reported = len(self._entries)

        header = "KV cache report" + (" (continued)" if first else "")
        lines = [f"{header}  (provider = truth; local = msgs-prefix + tools-match, template-dependent):"]

        # Lanes over the whole turn, not just the printed slice: a continued report
        # still compares against calls above it, so the legend has to name those too.
        lanes: list[tuple[str, str, str]] = []
        for e in self._entries:
            if _lane_of(e) not in lanes:
                lanes.append(_lane_of(e))
        # Only worth the noise when there is more than one. A dual-model turn is
        # where the reader most needs to know why a pass has no one to compare to:
        # it is the first call on its lane, not a broken prefix.
        multi_lane = len(lanes) > 1
        if multi_lane:
            lines.append("  lanes: " + "   ".join(f"L{n}={_lane_name(k)}" for n, k in enumerate(lanes, 1)))

        total_cached = 0
        total_prompt = 0

        for i in range(first, len(self._entries)):
            e = self._entries[i]
            lane = _lane_of(e)
            prev, cross_turn = self._find_prev(i, lane, e["label"])

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

            tag = f"L{lanes.index(lane) + 1} " if multi_lane else ""
            lines.append(f"  {tag}{e['label']:<28}  {provider_note}  |  {local_note}")

        # Over the calls printed above, not the whole conversation: on a continued
        # report those are one speaker's calls, which is the unit worth totalling.
        scope = "Totals (this report)" if first else "Totals"
        if total_prompt:
            pct_total = total_cached / total_prompt * 100
            lines.append(f"  {scope} — provider cached: {total_cached}/{total_prompt} tok ({pct_total:.1f}%)")
        else:
            lines.append(f"  {scope} — provider cached: N/A (server returned no usage data)")

        logger.info("\n".join(lines))

        if self._conversation_id:
            _prev_turn_entries[self._conversation_id] = list(self._entries)
