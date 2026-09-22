from collections.abc import Mapping, Sequence
from typing import Any

HEADING = "**Resolved Decisions**"


def decision_guidance_block(evaluations: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        f"{row.get('injection_label') or row.get('fragment_label') or row['fragment_id']}: {guidance}"
        for row in evaluations
        if (guidance := str(row.get("guidance") or "").strip())
    ]
    return f"{HEADING}\n" + "\n".join(lines) if lines else ""
