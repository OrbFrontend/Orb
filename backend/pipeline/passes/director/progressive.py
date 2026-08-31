"""Select and reset Director progressive-fragment state."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def select(values: Mapping[str, Any], interactive_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Keep only entries whose fragment has ``field_type == "progressive"``.

    Used both to seed prior progressive state (on ``director["progressive_fields"]``)
    and to derive this turn's output (on ``extra_fields``) — the operation is the
    same, so input and output stay symmetric.
    """
    progressive_ids = {f["id"] for f in interactive_fragments if f.get("field_type") == "progressive"}
    return {k: v for k, v in values.items() if k in progressive_ids}


def branch_baseline(history: Sequence[Mapping[str, Any]]) -> dict:
    """Return the progressive fields of the most recent assistant message in *history*.

    This is the branch-aware reset value: on regenerate/fork, progressive state
    rewinds to the grandparent (the last assistant message on the branch), not the
    linear-log value. Returns ``{}`` when there is no prior assistant message.
    """
    grandparent = next((m for m in reversed(history) if m["role"] == "assistant"), None)
    return (grandparent.get("progressive_fields") or {}) if grandparent else {}
