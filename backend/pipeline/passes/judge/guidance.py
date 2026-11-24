from collections.abc import Mapping, Sequence
from typing import Any, Literal

HEADING = "**Major Decisions**"


def decision_guidance_lines(evaluations: Sequence[Mapping[str, Any]], target: Literal["director", "writer"]) -> str:
    """The guidance lines of the resolved *evaluations* that inject into *target*, without their heading.

    A record without ``inject`` predates the setting and reaches both passes. Lines shared by both passes come first, so the
    Director's and the Writer's blocks open on the same bytes and a prefix cache can carry one into the other; decisions are
    independent, so their order carries no meaning.
    """
    shared: list[str] = []
    own: list[str] = []
    for row in evaluations:
        guidance = str(row.get("guidance") or "").strip()
        inject = row.get("inject") or "both"
        if guidance and inject in (target, "both"):
            label = row.get("injection_label") or row.get("fragment_label") or row["fragment_id"]
            (shared if inject == "both" else own).append(f"{label}: {guidance}")
    return "\n".join(shared + own)


def decision_guidance_block(evaluations: Sequence[Mapping[str, Any]], target: Literal["director", "writer"]) -> str:
    """*target*'s guidance lines under the Major Decisions heading; nothing when there are none."""
    return headed_decisions(decision_guidance_lines(evaluations, target))


def headed_decisions(lines: str) -> str:
    """*lines* under the Major Decisions heading; nothing when there are none."""
    return f"{HEADING}\n\n{lines}" if lines else ""
