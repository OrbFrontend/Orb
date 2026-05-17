"""
kv_tracker.py — Lightweight KV-cache hit/miss estimator shared across passes.

Serializes the full prompt (messages + tools) for each LLM call, then computes
the actual character-level common prefix between consecutive calls to estimate
cache reuse.  Character counts are a proxy for token counts — no tokeniser needed.

Cross-turn tracking: when a pass has no same-model predecessor in the current
turn (would show "baseline"), it falls back to the matching label from the
previous turn so inter-turn cache reuse is visible.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

# Per-conversation snapshot of the previous turn's entries, keyed by conversation_id.
_prev_turn_entries: dict[str, list[dict]] = {}


def _serialize_prompt(messages: list[dict], tools: list[dict] | None) -> str:
    """Compact JSON serialization of the full prompt for prefix comparison."""
    parts = [json.dumps(m, separators=(",", ":")) for m in messages]
    if tools:
        parts.append(json.dumps(tools, separators=(",", ":")))
    return "\n".join(parts)


def _common_prefix_len(a: str, b: str) -> int:
    """Length of the longest common prefix between two strings."""
    i = 0
    limit = min(len(a), len(b))
    while i < limit and a[i] == b[i]:
        i += 1
    return i


class _KVCacheTracker:
    def __init__(self, conversation_id: str | None = None):
        self._entries: list[dict] = []
        self._conversation_id = conversation_id
        self._prev_entries: list[dict] = list(_prev_turn_entries.get(conversation_id, [])) if conversation_id else []

    def record(
        self,
        label: str,
        messages: list[dict],
        tools: list[dict] | None,
        model: str = "",
    ) -> None:
        """Snapshot a single LLM call. Call once per pass (or per tool in the director)."""
        serialized = _serialize_prompt(messages, tools)
        self._entries.append(
            {
                "label": label,
                "model": model,
                "serialized": serialized,
                "total_chars": len(serialized),
                "tools_names": ([t.get("function", {}).get("name", "") for t in tools] if tools else []),
            }
        )

    def log_summary(self) -> None:
        if not self._entries:
            return

        lines = ["KV cache comparison  (serialized prompt overlap):"]
        total_saved = 0

        for i, e in enumerate(self._entries):
            e_model = e.get("model", "")
            # Look for same-model predecessor within this turn first.
            prev = next(
                (self._entries[j] for j in range(i - 1, -1, -1) if self._entries[j].get("model", "") == e_model),
                None,
            )
            cross_turn = False
            if prev is None and self._prev_entries:
                # Fall back to the same-label entry from the previous turn.
                prev = next(
                    (p for p in self._prev_entries if p["label"] == e["label"]),
                    None,
                )
                cross_turn = prev is not None

            if prev is None:
                overlap = 0
                cache_note = "baseline"
            else:
                prev_serialized = prev["serialized"]
                overlap = _common_prefix_len(prev_serialized, e["serialized"])
                if overlap > 0:
                    total_saved += overlap
                    pct = overlap / len(prev_serialized) * 100 if prev_serialized else 0
                    turn_tag = "prev-turn " if cross_turn else ""
                    cache_note = f"HIT  overlap={overlap} ({pct:.1f}%)  vs {turn_tag}{prev['label']!r}"
                else:
                    cache_note = "BUST  no_overlap"

            tail = e["total_chars"] - overlap

            lines.append(f"  {e['label']:<28}  total={e['total_chars']:7d}  " f"tail={tail:6d}  {cache_note}")

        lines.append(f"  Total estimated KV cache char savings: {total_saved}")
        logger.info("\n".join(lines))

        if self._conversation_id:
            _prev_turn_entries[self._conversation_id] = list(self._entries)
