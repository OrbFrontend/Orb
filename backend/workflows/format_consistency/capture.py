"""Opt-in capture of the markup normalizer's exact inputs, for offline evaluation.

The markup classifier (../RP-Markup-Classifier) is scored on the draft this hook
actually receives. Stored assistant messages are not that draft: they are the
hook's output, so a message the normalizer already rewrote looks consistent by
construction. When ``ORB_MARKUP_CAPTURE`` names a file, every post-pipeline call
appends one JSON line: the incoming draft, the baseline window it was judged
against (message ids and text, newest first), the heuristic's source/target and
action, and the text the hook emitted. Off when unset; never raises into the hook.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

ENV = "ORB_MARKUP_CAPTURE"
VERSION = 1


def _axes(style: Any) -> dict[str, str] | None:
    if style is None:
        return None
    return {"narration": style.narration.value, "dialogue": style.dialogue.value}


def _parent_id(history: Sequence[Mapping[str, Any]]) -> int | None:
    """The newest persisted history row: the branch tip the draft replies to."""
    for msg in reversed(history):
        mid = msg.get("id")
        if isinstance(mid, int):
            return mid
    return None


def _append(path: str, line: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


async def record(ctx: Any, *, window: Sequence[Mapping[str, Any]], draft: str, report: Any, output: str) -> None:
    """Append one capture row when ``ORB_MARKUP_CAPTURE`` is set."""
    path = os.environ.get(ENV)
    if not path:
        return
    try:
        row = {
            "v": VERSION,
            "captured_at": datetime.now(UTC).isoformat(),
            "conversation_id": ctx.conversation_id,
            "character_id": ctx.character_id,
            "parent_id": _parent_id(ctx.history),
            # The hook's own input, before the optional voice rewrite.
            "hook_input": ctx.draft,
            # What normalize_to_baseline received: the text to evaluate.
            "draft": draft,
            "baseline": [{"id": msg.get("id"), "content": msg.get("content", "")} for msg in window],
            "source": _axes(getattr(report, "source", None)),
            "target": _axes(getattr(report, "target", None)),
            "note": getattr(report, "note", None),
            "changed": bool(getattr(report, "changed", False)),
            "output": output,
        }
        await asyncio.to_thread(_append, path, json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        logger.exception("format-consistency: markup capture failed; continuing")
