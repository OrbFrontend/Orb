"""Score whether the draft repeats a subject's description from each recent reply, with the local pair comparer."""

from __future__ import annotations

from collections.abc import Sequence

from ....analysis.subjects import subject_pair_parts, subjects_input
from ....inference import local_ml


async def repeat_scores(draft: str, earlier: Sequence[str], categories: Sequence[str]) -> dict[str, list[float | None]]:
    """Per category, the comparer's repeat probability against each of *earlier* (newest first).

    One comparer read per earlier reply. A reply with no narration scores ``None``, which never counts as a repeat.
    """
    draft_narration = subjects_input(draft)
    narrations = [subjects_input(text) for text in earlier]
    readable = [i for i, narration in enumerate(narrations) if narration]
    reads = await local_ml.acompare_subjects([subject_pair_parts(narrations[i], draft_narration) for i in readable])
    by_reply = dict(zip(readable, reads, strict=True))
    return {
        category: [read[category] if (read := by_reply.get(i)) is not None else None for i in range(len(earlier))]
        for category in categories
    }
