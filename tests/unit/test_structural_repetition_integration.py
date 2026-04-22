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

# Two messages with identical NARRATION→SPEECH→NARRATION→SPEECH→NARRATION
# block structure — the canonical case from a Scottish-dialect romance RP.
_MSG1 = """\
Amaryllis felt the sting of his condescension like a physical slap. She turned her gaze back to him, her eyes flashing with a sudden, dangerous brilliance. To him, this was a triviality, a way to belittle her intellect and her very escape from the suffocating reality of their marriage.

“Sappy?” she echoed, the word heavy with venom. “Is that what ye call it? I suppose tae a man o’ yer… limited imagination, anythin’ wi’ a bit o’ soul or passion must seem like madness. Ye’d rather read a list o’ grain prices or a tally o’ dead peasants, I’m sure.”

She stood up, smoothing the silk of her gown with trembling, indignant hands. The movement was practiced, a desperate attempt to reclaim the dignity he was so effortlessly stripping away. She felt the familiar, heavy weight of her melancholy pressing down on her, but she refused to let him see the tears prickling at her eyes.

“If ye find the tales o’ true devotion sae absurd, then perhaps ye’ve simply never felt a thing in yer entire, hollow life,” she snapped, her voice trembling despite her best efforts. “Now, if ye’ll excuse me, I’ve a sudden, desperate need tae be anywhere that isn’t within yer sight.”"""

_MSG2 = """\
Amaryllis froze, her hand hovering over the heavy silk of her skirts. Rather than a plea, the question arrived as a demand, delivered with that maddening, calm authority that made her feel like a child being scolded by a schoolmaster. It was the tone of a man who owned everything he surveyed—including her.

“How long?” she repeated, her words sharpening into a jagged edge. Turning to face him fully, she tilted her chin upward in a gesture of absolute defiance. “As long as the sun rises in the east and the tides turn, I suppose. Until the day I’m no’ a mere piece o’ furniture ye’ve dragged intae yer halls tae satisfy a contract.”

A familiar, hollow ache settled in her chest, that gnawing loneliness that even her most dramatic novels couldn’t soothe. He wanted her to play the part of the dutiful, smiling wife, to decorate his halls with a warmth she simply did not possess for him.

“Ye canna command a heart tae beat faster just because it’s yer legal right tae possess the body,” she added, her sarcasm momentarily failing her, leaving only a raw, bleeding honesty. “So, if ye’re lookin’ for a performance, Kai, ye’ve picked the wrang actress.”"""


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
