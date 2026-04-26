"""
structural_repetition.py — Detect repetitive message-level block structures.

Public API:
    detect_structural_repetition(messages, similarity_threshold=0.75, min_complexity=2)
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

__all__ = ["detect_structural_repetition", "StructuralResult", "MessageStructure"]

# ---------- shared quote constants (keep in sync with your other modules) ----------
_OPEN_QUOTES = {"\u201c", "\u2018"}
_CLOSE_QUOTES = {"\u201d", "\u2019"}
_TOGGLE_QUOTES = {'"'}
_PARA_SPLIT = re.compile(r"\n\s*\n")
_SENT_SPLIT = re.compile(r"(?<=[.!?\u2026])\s+")

# ---------- dataclasses ----------


@dataclass
class MessageStructure:
    index: int
    signature: list[str]
    blocks: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class StructuralResult:
    is_repetitive: bool
    min_similarity: float
    mean_similarity: float
    shared_skeleton: list[str] | None
    messages: list[MessageStructure]


# ---------- sentence counting ----------


def _count_sentences(text: str) -> int:
    """Count sentences in a block of text.

    Non-empty text that contains no sentence terminator still counts as 1
    (it is a fragment or short imperative).  Empty text returns 0.
    """
    stripped = text.strip()
    if not stripped:
        return 0
    pieces = [s.strip() for s in _SENT_SPLIT.split(stripped) if s.strip()]
    return len(pieces) if pieces else 1


# ---------- block extraction ----------


def _find_quote_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    inside = False
    start = 0
    for i, ch in enumerate(text):
        if ch in _TOGGLE_QUOTES:
            if not inside:
                inside = True
                start = i
            else:
                spans.append((start, i + 1))
                inside = False
        elif ch in _OPEN_QUOTES:
            if not inside:
                inside = True
                start = i
        elif ch in _CLOSE_QUOTES:
            if inside:
                spans.append((start, i + 1))
                inside = False
    return spans


_EMPHASIS_RE = re.compile(
    r"(?<!\w)\*(?!\s)([^*\n]+?)\*(?!\w)"  # *thought*  (not bullet)
    r"|"
    r"(?<!\w)_(?!\s)([^_\n]+?)_(?!\w)",  # _thought_
)


def _find_emphasis_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for m in _EMPHASIS_RE.finditer(text):
        # Bullet guard: if this * is first non-space on its line and followed by space, skip
        if m.group(0).startswith("*"):
            line_start = text.rfind("\n", 0, m.start()) + 1
            prefix = text[line_start : m.start()]
            after_star = m.start() + 1
            if (
                prefix.strip() == ""
                and after_star < len(text)
                and text[after_star] in " \t"
            ):
                continue
        spans.append((m.start(), m.end()))
    return spans


def _extract_blocks(para: str) -> list[tuple[str, str]]:
    """Return ordered (type, text) blocks for a single paragraph."""
    quote_spans = _find_quote_spans(para)
    # Emphasis only outside quotes
    emphasis_spans = []
    prev_end = 0
    for qs, qe in sorted(quote_spans):
        emphasis_spans.extend(
            (s, e) for s, e in _find_emphasis_spans(para[prev_end:qs])
        )
        prev_end = qe
    emphasis_spans.extend((s, e) for s, e in _find_emphasis_spans(para[prev_end:]))

    # Merge and tag
    typed = [(s, e, "SPEECH") for s, e in quote_spans] + [
        (s, e, "EMPHASIS") for s, e in emphasis_spans
    ]
    typed.sort()

    blocks: list[tuple[str, str]] = []
    idx = 0
    for s, e, typ in typed:
        if idx < s:
            t = para[idx:s].strip()
            if t:
                blocks.append(("NARRATION", t))
        t = para[s:e].strip()
        if t:
            blocks.append((typ, t))
        idx = max(idx, e)
    if idx < len(para):
        t = para[idx:].strip()
        if t:
            blocks.append(("NARRATION", t))
    return blocks


def _collapse_signature(blocks: list[tuple[str, str]]) -> list[str]:
    """Collapse consecutive same-type blocks into counted signature tokens.

    Each token encodes ``TYPE:sentence_count`` so that, e.g., two narration
    sentences between speech blocks (``NARRATION:2``) are distinguished from
    one (``NARRATION:1``).  Sentence counts apply to *all* block types
    (SPEECH, NARRATION, EMPHASIS).
    """
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


# ---------- similarity ----------


def _sequence_similarity(a: list[str], b: list[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# ---------- public API ----------


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