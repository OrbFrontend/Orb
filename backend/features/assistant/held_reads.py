"""The reads a held context carries: run again for a follow-up to say what differs, and matched against a repeated read."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import ValidationError

from .contracts import AssistantTool, HeldRead, ToolFailure, encode_result

_DIFFERENCE_CHARS = 3000


def _difference(before: str, after: Mapping[str, Any]) -> dict[str, Any]:
    """The keys of *after* that differ from the result *before*; a list keeps only its rows that are not in the old one."""
    try:
        old = json.loads(before)
    except ValueError:
        return dict(after)
    if not isinstance(old, dict):
        return dict(after)
    shown: dict[str, Any] = {}
    for key, value in after.items():
        was = old.get(key)
        if value == was:
            continue
        if isinstance(value, list) and isinstance(was, list):
            rows = [row for row in value if row not in was]
            shown[key] = rows
            if len(rows) < len(value):
                shown[f"{key}_as_before"] = len(value) - len(rows)
        else:
            shown[key] = value
    return shown


async def read_again(reads: Sequence[HeldRead], tools: Mapping[str, AssistantTool], encode: Any) -> tuple[str, list[HeldRead]]:
    """Run each held read once more: what to tell the follow-up, and the reads it then holds.

    A result that differs is told by its difference when that is short, and is dropped from the held reads otherwise.
    """
    latest = {(read.tool, read.arguments): read for read in reads}
    same: list[str] = []
    differing: list[str] = []
    held: list[HeldRead] = []
    for read in latest.values():
        tool = tools.get(read.tool)
        label = f"{read.tool} {read.arguments}"
        if tool is None or tool.behavior != "read":
            continue
        try:
            output = await tool.run(tool.arguments.model_validate_json(read.arguments))
        except (ToolFailure, ValidationError) as exc:
            differing.append(f"{label} now fails: {str(exc)[:300]}")
            continue
        content = encode(output.data, output.summary)
        if content == read.content:
            same.append(label)
            held.append(read)
            continue
        difference = encode_result(_difference(read.content, output.data))
        if len(difference) > _DIFFERENCE_CHARS:
            differing.append(f"{label} differs too much to show here.")
            continue
        differing.append(f"{label} now differs in these keys: {difference}")
        held.append(HeldRead(read.tool, read.arguments, content, whole=False))
    lines: list[str] = []
    if same:
        lines.append(f"Run again just now, these earlier reads return exactly what they did: {'; '.join(same)}.")
    if differing:
        lines.append(
            "These return something else now. A list shows only its new or edited rows, and <key>_as_before counts the "
            "rows it still shares with the earlier result:\n" + "\n".join(differing)
        )
    return "\n".join(lines), held


def held_copy(reads: Sequence[HeldRead], tool: str, content: str) -> HeldRead | None:
    """The held read of *tool* whose whole result is exactly *content*, so a repeat need not send it twice."""
    return next((read for read in reads if read.whole and read.tool == tool and read.content == content), None)
