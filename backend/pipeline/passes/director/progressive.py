"""
passes/director/progressive.py — Director-local owner of progressive-fragment logic.

Progressive fragments are director-controlled fields whose value evolves
turn-over-turn. Descriptor-aware reduction now owns filtering and carry-forward;
this module retains only the branch-aware reset used by regenerate and fork.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


def branch_baseline(history: Sequence[Mapping[str, Any]]) -> dict:
    """Return the progressive fields of the most recent assistant message in *history*.

    This is the branch-aware reset value: on regenerate/fork, progressive state
    rewinds to the grandparent (the last assistant message on the branch), not the
    linear-log value. Returns ``{}`` when there is no prior assistant message.
    """
    grandparent = next((m for m in reversed(history) if m["role"] == "assistant"), None)
    return (grandparent.get("progressive_fields") or {}) if grandparent else {}
