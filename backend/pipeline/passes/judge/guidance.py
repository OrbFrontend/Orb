from collections.abc import Mapping, Sequence
from typing import Any, Literal

HEADING = "**Major Decisions**"


def decision_lines(evaluations: Sequence[Mapping[str, Any]], target: Literal["director", "writer"]) -> dict[str, str]:
    """The guidance line of each resolved decision in *evaluations* that injects into *target*, by fragment id, in run order.

    A record without ``inject`` predates the setting and reaches both passes.
    """
    lines: dict[str, str] = {}
    for row in evaluations:
        guidance = str(row.get("guidance") or "").strip()
        if guidance and (row.get("inject") or "both") in (target, "both"):
            label = row.get("injection_label") or row.get("fragment_label") or row["fragment_id"]
            lines[str(row["fragment_id"])] = f"{label}: {guidance}"
    return lines


def decision_guidance_block(evaluations: Sequence[Mapping[str, Any]], target: Literal["director", "writer"]) -> str:
    """*target*'s guidance lines under the Major Decisions heading; nothing when there are none."""
    lines = "\n".join(decision_lines(evaluations, target).values())
    return f"{HEADING}\n\n{lines}" if lines else ""
