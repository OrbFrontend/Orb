from collections.abc import Mapping, Sequence
from typing import Any

HEADING = "**Resolved Decisions**"


def decision_guidance_block(evaluations: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for row in evaluations:
        guidance = str(row.get("guidance") or "").strip()
        if guidance:
            label = row.get("injection_label") or row.get("fragment_label") or row["fragment_id"]
            lines.append(f"{label}: {guidance}")
    return f"{HEADING}\n" + "\n".join(lines) if lines else ""
