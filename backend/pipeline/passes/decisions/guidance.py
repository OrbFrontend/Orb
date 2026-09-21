"""Render resolved decisions as one model-facing block.

What reaches a prompt is the author's own words for the outcome that was
selected, under the author's own label. What does not reach a prompt: the
probability, the random draw, the resolution mode, and any provider error. A
rolled outcome is presented as a story constraint the author wrote, because that
is what it is -- the dice chose *which* constraint, not what the constraint says.

A decision also gets no authority it was not given. The block is a labelled
guidance list appended to the tail, the same shape an interactive fragment's
value takes in Scene Guidance; it is not a system instruction and cannot
countermand one.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

HEADING = "**Resolved Decisions**"


def decision_guidance_block(evaluations: Sequence[Mapping[str, Any]]) -> str:
    """The Scene Guidance sub-block for *evaluations*, or ``""``.

    Order is the order the stage published, which is fragment order -- so
    reordering decisions changes guidance order and budget priority, exactly as
    reordering any other fragment does.

    An empty selected output suppresses that decision entirely: an author who
    wrote guidance for one outcome and left the other blank meant "nothing to say
    in that case", and a label with nothing after it would say something.
    """
    lines = [
        f"{evaluation.get('injection_label') or evaluation.get('fragment_label') or evaluation['fragment_id']}: {guidance}"
        for evaluation in evaluations
        if (guidance := str(evaluation.get("guidance") or "").strip())
    ]
    return f"{HEADING}\n" + "\n".join(lines) if lines else ""


__all__ = ["HEADING", "decision_guidance_block"]
