"""
template_repetition.py — Detect repetitive sentence templates across many paragraphs.

Public API:
    detect_template_repetition(text, max_words=3, flag_threshold=3, similarity_threshold=0.5)
    TemplateResult, FlaggedTemplate  (dataclasses)

Logic:
    - Splits text into paragraphs and extracts sentences (similar to opening_monotony).
    - Strips dialogue to focus on narration only.
    - Extracts the first `max_words` words of each sentence as its "template".
    - Clusters templates by similarity (word overlap).
    - Flags templates that appear `flag_threshold` or more times.

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

from .text_segmentation import normalize_word, split_narration_sentences

DEBUG = "DEBUG_TEMPLATE_REPETITION" in os.environ

__all__ = [
    "detect_template_repetition",
    "TemplateResult",
    "FlaggedTemplate",
]

# ---------- public dataclasses ----------


@dataclass
class FlaggedTemplate:
    template: str
    count: int
    fraction: float
    sentences: list[str] = field(default_factory=list)


@dataclass
class TemplateResult:
    flagged_templates: list[FlaggedTemplate]
    all_templates: dict[str, int]
    total_sentences: int
    unique_templates: int
    repetition_score: float


# ---------- text processing ----------
# Paragraph/sentence/dialogue segmentation lives in text_segmentation so every
# audit pass splits text identically. `_split_sentences` strips dialogue.

_split_sentences = split_narration_sentences


# ---------- template analysis ----------


def _get_template(sentence: str, max_words: int) -> str | None:
    """Extract the template (first N words) from a sentence."""
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
    """Calculate word overlap (Jaccard) similarity between two templates."""
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
    """Check if two templates are similar based on word overlap or prefix match."""
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
    """Cluster sentences by similar templates.

    Returns a dict mapping canonical template -> list of (sentence, template) pairs.
    Uses the most frequent template in each cluster as the canonical form.
    """
    # Greedy clustering first
    clusters: list[list[tuple[str, str]]] = []

    for sent, tmpl in zip(sentences, templates, strict=True):
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
    """Detect repetitive sentence templates across many paragraphs.

    Args:
        text: The text to analyze.
        max_words: Maximum number of words to use as a template.
        flag_threshold: Minimum count of similar templates to flag.
        similarity_threshold: Minimum similarity (0-1) for templates to be considered similar.

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
