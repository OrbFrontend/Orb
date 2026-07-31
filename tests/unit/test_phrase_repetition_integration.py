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


async def test_phrase_repetition_detected_through_contextual_audit():
    """A distinctive phrase echoed across three messages must be flagged."""
    draft = "She met the shadowed red eyes across the crowded table without a word."

    report, text = await _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[_PREV1, _PREV2],
    )
    assert report.phrase_result is not None, "phrase_result should be set when previous messages are provided"
    phrases = [p.phrase for p in report.phrase_result.flagged_phrases]
    assert "shadowed red eyes" in phrases, f"Expected 'shadowed red eyes' to be flagged, got {phrases}"
    # The report must include the draft sentence so the editor can patch it.
    assert draft in text


async def test_phrase_repetition_no_false_positive_when_draft_is_distinct():
    """A draft that shares no distinctive phrase must not be flagged."""
    report, _ = await _run_contextual_audit(
        draft="A wholly unrelated sentence about the weather today.",
        phrase_bank=[],
        previous_assistant_msgs=[_PREV1, _PREV2],
    )
    assert report.phrase_result is not None
    assert not report.phrase_result.flagged_phrases, (
        f"Expected no flagged phrases, got {[p.phrase for p in report.phrase_result.flagged_phrases]}"
    )


async def test_phrase_repetition_two_word_pair_below_threshold_not_flagged():
    """A two-word pair echoed across only two messages (draft + one previous)
    must NOT be flagged: short phrases keep the higher three-message threshold,
    since a 2-word match is easily a coincidence."""
    prev1 = "His eyes burned with quiet fury at the news."
    draft = "A quiet fury settled over the room as he left."

    report, _ = await _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[prev1],
    )
    assert report.phrase_result is not None
    assert not report.phrase_result.flagged_phrases, (
        "A two-word pair shared by only two messages must not be flagged at the "
        f"three-message threshold, got {[p.phrase for p in report.phrase_result.flagged_phrases]}"
    )


async def test_phrase_repetition_long_phrase_flagged_at_two_messages():
    """A three-word phrase echoed across only two messages (draft + one previous)
    MUST be flagged: longer phrases are distinctive enough that a single repeat is
    damning, so they use the lower two-message threshold."""
    draft = "She met the shadowed red eyes across the crowded table without a word."

    report, _ = await _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[_PREV1],
    )
    assert report.phrase_result is not None
    phrases = [p.phrase for p in report.phrase_result.flagged_phrases]
    assert "shadowed red eyes" in phrases, (
        f"A distinctive 3-word phrase shared by two messages must be flagged at the two-message threshold, got {phrases}"
    )


async def test_phrase_repetition_detects_two_word_pair():
    """A distinctive two-content-word pair echoed across three messages must be
    flagged even when the messages share no 3-word overlap. Proves min_n=2 is in
    effect end-to-end."""
    prev1 = "His eyes burned with quiet fury at the news."
    prev2 = "She spoke with quiet fury, voice low and even."
    draft = "A quiet fury settled over the room as he left."

    report, _ = await _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[prev1, prev2],
    )
    assert report.phrase_result is not None
    phrases = [p.phrase for p in report.phrase_result.flagged_phrases]
    assert "quiet fury" in phrases, f"Expected 'quiet fury' to be flagged, got {phrases}"


async def test_phrase_repetition_two_word_pair_needs_two_content_words():
    """A bigram echoed across three messages that is one stopword + one content
    word (e.g. 'his gaze') must NOT be flagged. The min_content_words=2 floor
    keeps 2-word detection from degenerating into a single-word match."""
    prev1 = "He held his gaze on the horizon for a while."
    prev2 = "She felt his gaze settle on her shoulders."
    draft = "I returned his gaze without a single word."

    report, _ = await _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[prev1, prev2],
    )
    assert report.phrase_result is not None
    assert not report.phrase_result.flagged_phrases, (
        "A content+stopword bigram has only one content word and must not be "
        f"flagged, got {[p.phrase for p in report.phrase_result.flagged_phrases]}"
    )


async def test_phrase_repetition_only_flags_echoes_in_draft():
    """A phrase repeated only among previous messages (absent from the draft)
    must not be flagged, since require_last_message focuses on the draft."""
    prev1 = "His shadowed red eyes flickered in the firelight as the storm rolled in."
    prev2 = "Behind the iron mask, his shadowed red eyes burned with quiet fury."
    draft = "The morning brought soft rain and the smell of wet stone."

    report, _ = await _run_contextual_audit(
        draft=draft,
        phrase_bank=[],
        previous_assistant_msgs=[prev1, prev2],
    )
    assert report.phrase_result is not None
    assert not report.phrase_result.flagged_phrases, (
        "Phrases shared only between previous messages must not be flagged when "
        f"absent from the draft, got {[p.phrase for p in report.phrase_result.flagged_phrases]}"
    )
