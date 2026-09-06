"""Regression tests for detect_contrastive_negation.

2026-07-18: curly-apostrophe contractions ("doesn’t") and do-support clauses
whose verb the suffix tagger can't see ("we time it") were missed.

2026-09-06: the "not to X, but to Y" frame was missed wholesale — "not to"
bailed out early, and "not to push you away" read its object as a subject.
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
    # "not to X, but to Y": both arms are infinitives, so neither is a clause.
    "Her fingers tighten around your wrist, not to push you away, but to ground herself.",
    "She reached out not to comfort him but to stop him.",  # no commas
    "He speaks not to inform you, but rather to unsettle you.",  # hedged arm
    "She moved not merely to escape, but to warn them.",
    "It's important not to rush, but to be careful.",
    # A coordinate clause tacked on the end doesn't undo the contrast.
    "She wanted not to leave, but to be asked to stay, and he knew it.",
]

MISSES = [
    "It is not only fast but also cheap.",
    "I told him not to go, but he went anyway.",
    "I'm not sure, but I think it works.",
    "The report isn't done, but the deadline is Friday.",
    "He isn't tall, she is short.",
    "Isn't that odd?",
    "I don't like rain, but I brought an umbrella.",
    # "not to" shapes whose "but" opens a clause instead of a second infinitive.
    "She tried not to laugh, but the joke was too good.",
    "I told him not to go, but to my surprise he went anyway.",  # PP, not infinitive
    "I told him not to go, but to be fair, he had no choice.",  # parenthetical + clause
    "It is not only there to help but also to hinder.",  # correlative, not contrastive
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
