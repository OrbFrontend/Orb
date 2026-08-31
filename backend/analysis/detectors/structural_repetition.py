"""Detect repeated block-level message structure."""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field

from ..text.text_segmentation import (
    PARA_SPLIT as _PARA_SPLIT,
)
from ..text.text_segmentation import (
    count_sentences as _count_sentences,
)
from ..text.text_segmentation import (
    extract_blocks as _extract_blocks,
)

__all__ = ["detect_structural_repetition", "StructuralResult", "MessageStructure"]


@dataclass(slots=True)
class MessageStructure:
    index: int
    signature: list[str]
    blocks: list[tuple[str, str]] = field(default_factory=list)


@dataclass(slots=True)
class StructuralResult:
    is_repetitive: bool
    min_similarity: float
    mean_similarity: float
    shared_skeleton: list[str] | None
    messages: list[MessageStructure]


def _collapse_signature(blocks: list[tuple[str, str]]) -> list[str]:
    """Convert blocks into a compact type-and-sentence signature."""
    if not blocks:
        return []
    sig: list[str] = []
    current_type = blocks[0][0]
    current_count = _count_sentences(blocks[0][1])
    for typ, text in blocks[1:]:
        count = _count_sentences(text)
        if typ == current_type:
            current_count += count
        else:
            sig.append(f"{current_type}:{current_count}")
            current_type = typ
            current_count = count
    sig.append(f"{current_type}:{current_count}")
    return sig


def _sequence_similarity(a: list[str], b: list[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def detect_structural_repetition(
    messages: list[str],
    similarity_threshold: float = 0.75,
    min_complexity: int = 2,
) -> StructuralResult:
    parsed: list[MessageStructure] = []

    for i, raw in enumerate(messages):
        blocks: list[tuple[str, str]] = []
        for para in _PARA_SPLIT.split(raw.strip()):
            para = para.strip()
            if para:
                blocks.extend(_extract_blocks(para))
        sig = _collapse_signature(blocks)
        parsed.append(MessageStructure(index=i, signature=sig, blocks=blocks))

    n = len(parsed)
    if n < 2:
        return StructuralResult(
            is_repetitive=False,
            min_similarity=0.0,
            mean_similarity=0.0,
            shared_skeleton=parsed[0].signature if parsed else None,
            messages=parsed,
        )

    # Pairwise similarities
    sims = [[0.0] * n for _ in range(n)]
    min_sim = 1.0
    total = 0.0
    count = 0

    for i in range(n):
        sims[i][i] = 1.0
        for j in range(i + 1, n):
            s = _sequence_similarity(parsed[i].signature, parsed[j].signature)
            sims[i][j] = sims[j][i] = s
            min_sim = min(min_sim, s)
            total += s
            count += 1

    mean_sim = total / count if count else 1.0

    # Complexity guard: ignore trivial pure-narration windows
    distinct_types = {t.split(":")[0] for m in parsed for t in m.signature}
    complex_enough = len(distinct_types) >= min_complexity

    is_rep = complex_enough and min_sim >= similarity_threshold

    skeleton = None
    if is_rep:
        # Shortest signature as the canonical skeleton
        skeleton = min((m.signature for m in parsed), key=len)

    return StructuralResult(
        is_repetitive=is_rep,
        min_similarity=round(min_sim, 4),
        mean_similarity=round(mean_sim, 4),
        shared_skeleton=skeleton,
        messages=parsed,
    )
