"""Tests for choosing and encoding the advanced-cloning reference excerpt."""

from __future__ import annotations

import os

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.spark_tts import (  # noqa: E402
    catalog,
    reference,
    silence,
    tokens,
)

RATE = reference.SAMPLE_RATE


def _clip(*parts: tuple[str, float]) -> np.ndarray:
    """Concatenate ``("speech"|"pause", seconds)`` parts: a tone, or silence."""
    pieces = []
    for kind, seconds in parts:
        n = int(RATE * seconds)
        if kind == "speech":
            pieces.append((0.3 * np.sin(np.arange(n) * 2 * np.pi * 180 / RATE)).astype(np.float32))
        else:
            pieces.append(np.zeros(n, dtype=np.float32))
    return np.concatenate(pieces)


def _seconds(ranges) -> float:
    return sum(end - start for start, end in ranges) / RATE


def _longest_pause(audio: np.ndarray) -> float:
    quiet = ~silence.speech_mask(audio)
    longest = run = 0
    for frame_is_quiet in quiet:
        run = run + 1 if frame_is_quiet else 0
        longest = max(longest, run)
    return longest * reference.FRAME / RATE


def test_a_short_clip_is_taken_whole_with_a_little_edge():
    clip = _clip(("pause", 1.0), ("speech", 3.0), ("pause", 1.0))
    (start, end), *rest = reference.excerpt_ranges(clip)
    assert not rest
    assert start == pytest.approx(RATE * (1.0 - reference.EDGE_MS / 1000), abs=reference.FRAME)
    assert end == pytest.approx(RATE * (4.0 + reference.EDGE_MS / 1000), abs=reference.FRAME)


def test_long_pauses_inside_the_excerpt_are_shortened():
    """A dramatic silence in the reference is copied as a habit, and generation
    trails off instead of ending: measured as whole lines running to budget."""
    clip = _clip(("speech", 2.5), ("pause", 2.0), ("speech", 2.5), ("pause", 1.5), ("speech", 2.5))
    excerpt = reference.select_excerpt(clip)
    assert len(excerpt) / RATE == pytest.approx(7.5 + 2 * reference.KEEP_PAUSE_MS / 1000, abs=0.05)
    assert _longest_pause(excerpt) <= reference.KEEP_PAUSE_MS / 1000 + 0.02


def test_the_excerpt_opens_and_closes_at_a_break_rather_than_mid_clause():
    """A 250 ms gap is a breath inside a sentence; starting there opens the
    excerpt mid-thought, which Whisper then mishears."""
    clip = _clip(
        ("speech", 1.5),
        ("pause", 0.25),
        ("speech", 4.0),
        ("pause", 0.25),
        ("speech", 4.0),  # 1.75-10.0 is the most speech that fits, but opens mid-sentence
        ("pause", 1.0),
        ("speech", 3.0),  # 11.0: a sentence starts
        ("pause", 0.25),
        ("speech", 3.0),
        ("pause", 1.0),
        ("speech", 1.0),
        ("pause", 0.5),
    )
    ranges = reference.excerpt_ranges(clip)
    assert ranges[0][0] / RATE == pytest.approx(11.0 - reference.EDGE_MS / 1000, abs=0.03)
    assert ranges[-1][1] / RATE == pytest.approx(19.25 + reference.EDGE_MS / 1000, abs=0.03)


def test_a_break_is_not_worth_most_of_the_excerpt():
    """Long sentences leave only a short run between breaks; the longer
    mid-sentence run carries more delivery and wins."""
    clip = _clip(
        ("speech", 2.0),
        ("pause", 0.25),
        ("speech", 4.5),
        ("pause", 0.25),
        ("speech", 4.5),
        ("pause", 1.0),
        ("speech", 2.0),
    )
    ranges = reference.excerpt_ranges(clip)
    assert ranges[0][0] / RATE == pytest.approx(2.25 - reference.EDGE_MS / 1000, abs=0.03)
    assert _seconds(ranges) == pytest.approx(9.0 + reference.KEEP_PAUSE_MS / 1000 + 2 * reference.EDGE_MS / 1000, abs=0.05)


def test_a_clip_spoken_without_breaks_still_yields_an_excerpt():
    """Continuous speech is cut at its quietest point rather than refused."""
    rng = np.random.default_rng(0)
    speech = _clip(("speech", 30.0))
    speech *= (0.6 + 0.4 * np.sin(np.arange(speech.size) * 2 * np.pi * 0.7 / RATE) ** 2).astype(np.float32)
    speech += rng.standard_normal(speech.size).astype(np.float32) * 0.001
    ranges = reference.excerpt_ranges(speech)
    assert 2.0 <= _seconds(ranges) <= reference.TARGET_SECONDS + 2 * reference.EDGE_MS / 1000


@pytest.mark.parametrize(
    "clip",
    [
        _clip(("pause", 5.0)),
        _clip(("speech", 1.0), ("pause", 3.0)),
        _clip(("speech", 0.8), ("pause", 1.0), ("speech", 0.8), ("pause", 20.0)),
    ],
)
def test_too_little_speech_is_refused_with_a_reason(clip):
    with pytest.raises(reference.NoUsableExcerpt, match="seconds of clear speech"):
        reference.excerpt_ranges(clip)


def test_every_excerpt_fits_the_prompt_ceiling():
    clip = _clip(*[part for _ in range(12) for part in (("speech", 2.2), ("pause", 0.5))])
    assert _seconds(reference.excerpt_ranges(clip)) * tokens.SEMANTIC_RATE <= tokens.MAX_REFERENCE_TOKENS


@pytest.mark.real_model_dir
@pytest.mark.skipif(
    not os.path.exists(catalog.semantic_tokenizer_path()),
    reason="reference reader not downloaded (scripts/export_spark_semantic_tokenizer.py)",
)
def test_the_semantic_tokenizer_reads_fifty_tokens_a_second():
    pytest.importorskip("onnxruntime")
    rng = np.random.default_rng(1)
    signal = (rng.standard_normal(RATE * 3) * 0.1).astype(np.float32)
    try:
        got = reference.semantic_tokens(signal)
        again = reference.semantic_tokens(signal)
    finally:
        reference.release()
    assert abs(len(got) - 3 * tokens.SEMANTIC_RATE) <= 2
    assert tokens.validate_reference_tokens(got) == got
    assert got == again
