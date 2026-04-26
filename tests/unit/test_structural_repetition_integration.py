"""
test_structural_repetition_integration.py — Regression test for structural
repetition detection through the _run_contextual_audit integration path.

The bug: _run_contextual_audit passed full_text (previous msgs + draft
concatenated) as the `text` arg to run_audit, which forwarded it as the
"current message" to detect_structural_repetition.  The detector then compared
individual previous messages against a doubled-up blob, collapsing similarity
to ~0.67 and suppressing the flag.
"""

from __future__ import annotations

from backend.passes.editor.editor import _run_contextual_audit

# Short synthetic messages with identical block-type sequences AND identical
# sentence counts per block, so the detector should flag them as repetitive.
_MSG1 = """\
Kael looked around. "Let me see." He paused. "This is bad."
"What now?" Lira asked. "We run."
"""

_MSG2 = """\
Mira checked her bag. "One moment." She smiled. "All good."
"Ready?" Jon asked. "Yes."
"""


def test_structural_repetition_detected_through_contextual_audit():
    """_run_contextual_audit must flag identical-structure messages.

    Previously this returned is_repetitive=False because the draft was passed
    to detect_structural_repetition as part of a concatenated full_text blob
    rather than as a standalone message.
    """
    report, _ = _run_contextual_audit(
        draft=_MSG2,
        phrase_bank=[],
        previous_assistant_msgs=[_MSG1],
    )
    sr = report.structural_repetition_result
    assert (
        sr is not None
    ), "structural_repetition_result should be set when previous messages are provided"
    assert sr.is_repetitive, (
        f"Expected is_repetitive=True but got False "
        f"(min_sim={sr.min_similarity}, mean_sim={sr.mean_similarity}). "
        "The draft and previous message share an identical block structure."
    )


def test_structural_repetition_no_false_positive_different_sentence_counts():
    """Messages with different sentence counts per block must NOT be flagged.

    Splitting by sentences (not paragraphs) means that two messages sharing
    the same block-type order but differing in sentence counts per block
    produce different signatures and should not match.
    """
    msg1 = '"Let me see." Kael looked. Then he said. "This is bad."'
    msg2 = '"I told you so." Lira replied. "I\'m never wrong."'

    report, _ = _run_contextual_audit(
        draft=msg2,
        phrase_bank=[],
        previous_assistant_msgs=[msg1],
    )
    sr = report.structural_repetition_result
    assert sr is not None
    assert not sr.is_repetitive, (
        f"Expected is_repetitive=False but got True "
        f"(min_sim={sr.min_similarity}, mean_sim={sr.mean_similarity}). "
        "Msg1 has SPEECH:1 NARRATION:2 SPEECH:1, Msg2 has SPEECH:1 NARRATION:1 SPEECH:1."
    )
