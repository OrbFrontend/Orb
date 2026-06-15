"""
test_phrase_repetition_integration.py — Regression test for exact phrase
repetition detection through the _run_contextual_audit integration path.

The phrase_repetition detector is a cross-message check: it flags distinctive
n-grams that the current draft echoes from previous assistant messages. It runs
with require_last_message=True, so only phrases present in the draft surface.
"""

from __future__ import annotations

from backend.pipeline.passes.editor.editor import _run_contextual_audit

# The default threshold is 3 messages, so a flag needs the draft plus two
# previous messages all carrying the same distinctive phrase.
_PREV1 = "His shadowed red eyes flickered in the firelight as the storm rolled in."
_PREV2 = "Behind the iron mask, his shadowed red eyes burned with quiet fury."


def test_phrase_repetition_detected_through_contextual_audit():
    """A distinctive phrase echoed across three messages must be flagged."""
    draft = "She met the shadowed red eyes across the crowded table without a word."

    report, text = _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[_PREV1, _PREV2],
    )
    assert report.phrase_result is not None, "phrase_result should be set when previous messages are provided"
    phrases = [p.phrase for p in report.phrase_result.flagged_phrases]
    assert "shadowed red eyes" in phrases, f"Expected 'shadowed red eyes' to be flagged, got {phrases}"
    # The report must include the draft sentence so the editor can patch it.
    assert draft in text


def test_phrase_repetition_no_false_positive_when_draft_is_distinct():
    """A draft that shares no distinctive phrase must not be flagged."""
    report, _ = _run_contextual_audit(
        draft="A wholly unrelated sentence about the weather today.",
        phrase_bank=[],
        previous_assistant_msgs=[_PREV1, _PREV2],
    )
    assert report.phrase_result is not None
    assert not report.phrase_result.flagged_phrases, (
        f"Expected no flagged phrases, got {[p.phrase for p in report.phrase_result.flagged_phrases]}"
    )


def test_phrase_repetition_below_threshold_not_flagged():
    """An echo across only two messages (draft + one previous) must NOT be
    flagged, since the default threshold is three messages."""
    draft = "She met the shadowed red eyes across the crowded table without a word."

    report, _ = _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[_PREV1],
    )
    assert report.phrase_result is not None
    assert not report.phrase_result.flagged_phrases, (
        "A phrase shared by only two messages must not be flagged at the "
        f"three-message threshold, got {[p.phrase for p in report.phrase_result.flagged_phrases]}"
    )


def test_phrase_repetition_only_flags_echoes_in_draft():
    """A phrase repeated only among previous messages (absent from the draft)
    must not be flagged, since require_last_message focuses on the draft."""
    prev1 = "His shadowed red eyes flickered in the firelight as the storm rolled in."
    prev2 = "Behind the iron mask, his shadowed red eyes burned with quiet fury."
    draft = "The morning brought soft rain and the smell of wet stone."

    report, _ = _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[prev1, prev2],
    )
    assert report.phrase_result is not None
    assert not report.phrase_result.flagged_phrases, (
        "Phrases shared only between previous messages must not be flagged when "
        f"absent from the draft, got {[p.phrase for p in report.phrase_result.flagged_phrases]}"
    )
