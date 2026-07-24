"""
template_repetition.py — Detect repetitive sentence-opening templates spread
across many paragraphs.

Similar to opening_monotony, but looser: instead of requiring a strict
consecutive run, it clusters narration sentences by similar openings across the
whole response. Useful for catching recurring openers that aren't adjacent.

Public API:
    detect_template_repetition(text, max_words=3, flag_threshold=3, similarity_threshold=0.5)
    TemplateResult, FlaggedTemplate  (dataclasses)

How it works:
    - Strip dialogue and split the remaining narration into sentences.
    - Take the first max_words words of each sentence as its "template".
    - Cluster templates by word-overlap similarity (Jaccard) or shared prefix.
    - Flag any cluster that reaches flag_threshold or more sentences.

Example target pattern:
    "The question hangs in the air..."
    (several paragraphs later...)
    "The question is heavy..."
    ^^^ flagged as "the question" template repetition
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field

from ..text.lexical import normalize_word
from ..text.text_segmentation import split_narration_sentences

DEBUG = "DEBUG_TEMPLATE_REPETITION" in os.environ

__all__ = [
    "detect_template_repetition",
    "TemplateResult",
    "FlaggedTemplate",
]

# ---------- public dataclasses ----------


@dataclass(slots=True)
class FlaggedTemplate:
    template: str
    count: int
    fraction: float
    sentences: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TemplateResult:
    flagged_templates: list[FlaggedTemplate]
    all_templates: dict[str, int]
    total_sentences: int
    unique_templates: int
    repetition_score: float


# ---------- text processing ----------
# Segmentation lives in text_segmentation so every detector splits text the
# same way. _split_sentences strips dialogue before splitting into sentences.

_split_sentences = split_narration_sentences


# ---------- template analysis ----------


def _get_template(sentence: str, max_words: int) -> str | None:
    """Return the first N normalized words of a sentence as its template, or
    None if the sentence is too short to produce a meaningful template."""
    words = sentence.split()
    if len(words) < 3:
        return None
    # Take up to max_words words
    template_words = words[:max_words]
    normalized = [normalize_word(w) for w in template_words]
    # Filter out empty words after normalization
    normalized = [w for w in normalized if w]
    if len(normalized) < 3:
        return None
    return " ".join(normalized)


def _word_overlap_similarity(t1: str, t2: str) -> float:
    """Jaccard similarity between two template strings (word-level intersection over union)."""
    words1 = set(t1.split())
    words2 = set(t2.split())

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    if not union:
        return 0.0

    return len(intersection) / len(union)


def _templates_similar(t1: str, t2: str, threshold: float, max_words: int) -> bool:
    """True if two templates are similar enough to cluster together.

    Two templates match if they share a common prefix of at least (max_words - 1)
    words, or if their Jaccard word-overlap meets the threshold.
    """
    if t1 == t2:
        return True

    words1 = t1.split()
    words2 = t2.split()

    # Check for prefix match, but only if shorter template is substantial
    # (prevents "the" from matching everything)
    min_len = min(len(words1), len(words2))
    min_prefix_len = max(1, max_words - 1)  # Require at least max_words-1 words

    if min_len >= min_prefix_len and words1[:min_len] == words2[:min_len]:
        return True

    # Word overlap similarity
    return _word_overlap_similarity(t1, t2) >= threshold


def _cluster_templates(
    sentences: list[str],
    templates: list[str | None],
    similarity_threshold: float,
    max_words: int,
) -> dict[str, list[tuple[str, str]]]:
    """Group sentences into clusters of similar templates.

    Uses greedy clustering: each sentence joins the first existing cluster whose
    canonical template is similar enough; otherwise it starts a new cluster.
    After clustering, the most frequent template in each cluster becomes the
    canonical form.

    Returns a dict mapping canonical template -> list of (sentence, template) pairs.
    """
    # Greedy clustering first
    clusters: list[list[tuple[str, str]]] = []

    for sent, tmpl in zip(sentences, templates):
        if tmpl is None:
            continue

        # Try to find an existing cluster
        found_cluster = None
        for cluster in clusters:
            # Use first template as canonical for similarity check
            canonical = cluster[0][1]
            if _templates_similar(tmpl, canonical, similarity_threshold, max_words):
                cluster.append((sent, tmpl))
                found_cluster = cluster
                break

        if not found_cluster:
            # Create new cluster
            clusters.append([(sent, tmpl)])

    # Re-canonicalize: use most frequent template in each cluster as canonical
    result: dict[str, list[tuple[str, str]]] = {}
    for cluster in clusters:
        # Count template frequencies
        counts: dict[str, int] = defaultdict(int)
        for _, tmpl in cluster:
            counts[tmpl] += 1

        # Choose most frequent (or shortest as tiebreaker)
        best = max(counts.keys(), key=lambda t: (counts[t], -len(t)))
        result[best] = cluster

    return result


def detect_template_repetition(
    text: str,
    max_words: int = 3,
    flag_threshold: int = 3,
    similarity_threshold: float = 0.5,
) -> TemplateResult:
    """Detect narration sentences whose openings follow the same template too often.

    Args:
        text: The text to analyze.
        max_words: How many words to take from the start of each sentence as its template.
        flag_threshold: Minimum number of sentences sharing a template before it's flagged.
        similarity_threshold: Minimum Jaccard word-overlap for two templates to be
            considered the same (0–1).

    Returns:
        TemplateResult with flagged templates and statistics.
    """
    sentences = _split_sentences(text)
    if DEBUG:
        sys.stderr.write(f"[template_repetition] sentences: {sentences}\n")

    total = len(sentences)
    if total == 0:
        return TemplateResult([], {}, 0, 0, 0.0)

    # Extract templates from each sentence
    templates: list[str | None] = [_get_template(s, max_words) for s in sentences]
    if DEBUG:
        sys.stderr.write(f"[template_repetition] templates: {templates}\n")

    # Count exact templates
    exact_counts: dict[str, int] = {}
    for tmpl in templates:
        if tmpl:
            exact_counts[tmpl] = exact_counts.get(tmpl, 0) + 1

    # Cluster templates by similarity
    clusters = _cluster_templates(sentences, templates, similarity_threshold, max_words)

    if DEBUG:
        sys.stderr.write(f"[template_repetition] clusters: {clusters}\n")

    # Find flagged templates (clusters with count >= flag_threshold)
    flagged: list[FlaggedTemplate] = []

    for canonical, items in clusters.items():
        count = len(items)
        if count >= flag_threshold:
            # Get sentences in this cluster
            cluster_sentences = [sent for sent, _ in items]
            flagged.append(
                FlaggedTemplate(
                    template=canonical,
                    count=count,
                    fraction=round(count / total, 4),
                    sentences=cluster_sentences,
                )
            )

    # Sort by count descending
    flagged.sort(key=lambda x: x.count, reverse=True)

    # Calculate repetition score based on clustered counts
    # (counts clusters with 2+ sentences, not just exact matches)
    repeated_count = sum(len(items) for items in clusters.values() if len(items) >= 2)
    repetition_score = round(repeated_count / total, 4) if total else 0.0

    return TemplateResult(
        flagged_templates=flagged,
        all_templates=exact_counts,
        total_sentences=total,
        unique_templates=len(clusters),
        repetition_score=repetition_score,
    )
