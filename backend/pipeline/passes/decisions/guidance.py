from collections.abc import Mapping, Sequence
from typing import Any

HEADING = "**Resolved Decisions**"


def decision_guidance_block(evaluations: Sequence[Mapping[str, Any]]) -> str:
    lines: list[str] = []
    for row in evaluations:
        guidance = str(row.get("guidance") or "").strip()
        facet_lines = [
            f"  {facet.get('label') or facet.get('key')}: {facet_guidance}"
            for facet in row.get("facets", [])
            if isinstance(facet, Mapping) and (facet_guidance := str(facet.get("guidance") or "").strip())
        ]
        if guidance or facet_lines:
            label = row.get("injection_label") or row.get("fragment_label") or row["fragment_id"]
            if guidance:
                lines.append(f"{label}: {guidance}")
            else:
                lines.append(f"{label}:")
            lines.extend(facet_lines)
    return f"{HEADING}\n" + "\n".join(lines) if lines else ""
