"""Regression tests for detect_contrastive_negation.

2026-07-18: curly-apostrophe contractions ("doesn’t") and do-support clauses
whose verb the suffix tagger can't see ("we time it") were missed.
"""

from __future__ import annotations

import pytest

from backend.analysis.detectors.contrastive_negation import detect_contrastive_negation

HITS = [
    "You aren't just stealing; you're erasing a symbol.",
    "We don't just hit it; we time it for the rush.",
    "It doesn’t fade; it is strangled.",  # curly apostrophe
    "It doesn't fade; it is strangled.",
    "It's not a bug, but a feature.",
    "This isn't a setback, it is an opportunity.",
    "He doesn't just give up; he breaks down.",
]

MISSES = [
    "It is not only fast but also cheap.",
    "I told him not to go, but he went anyway.",
    "I'm not sure, but I think it works.",
    "The report isn't done, but the deadline is Friday.",
    "He isn't tall, she is short.",
    "Isn't that odd?",
    "I don't like rain, but I brought an umbrella.",
]


@pytest.mark.parametrize("text", HITS)
def test_detects(text):
    assert detect_contrastive_negation(text), text


@pytest.mark.parametrize("text", MISSES)
def test_ignores(text):
    assert not detect_contrastive_negation(text), text


def test_curly_apostrophe_sentence_reported_verbatim():
    """patching.py filters hits by substring into the original text — the
    reported sentence must keep the original curly apostrophe."""
    text = "It doesn’t fade; it is strangled."
    hits = detect_contrastive_negation(text)
    assert hits and hits[0]["sentence"] in text
