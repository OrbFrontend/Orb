"""Tests for Spark-TTS silence normalization."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.spark_tts import enroll, silence  # noqa: E402


def test_frame_rms_and_speech_mask_use_mel_hop_frames():
    signal = np.concatenate(
        [
            np.zeros(320, dtype=np.float32),
            np.full(320, 0.02, dtype=np.float32),
            np.full(160, 0.005, dtype=np.float32),
        ]
    )
    assert np.allclose(silence.frame_rms(signal, 320, 320), [0.0, 0.02, 0.005])
    assert silence.speech_mask(signal).tolist() == [False, True, False]


def test_trim_silence_removes_edges_and_clamps_long_interior_runs():
    speech_a = np.full(6400, 0.2, dtype=np.float32)
    speech_b = np.full(6400, -0.2, dtype=np.float32)
    signal = np.concatenate(
        [
            np.zeros(640, dtype=np.float32),
            speech_a,
            np.zeros(6400, dtype=np.float32),
            speech_b,
            np.zeros(960, dtype=np.float32),
        ]
    )
    got = silence.trim_silence(signal)
    expected = np.concatenate([speech_a, np.zeros(3200, dtype=np.float32), speech_b])
    assert np.array_equal(got, expected)


def test_trim_silence_leaves_a_300ms_pause_unchanged():
    speech = np.full(16000, 0.2, dtype=np.float32)
    pause = np.zeros(4800, dtype=np.float32)
    signal = np.concatenate([speech, pause, speech])
    assert np.array_equal(silence.trim_silence(signal), signal)


def test_trim_silence_cuts_only_at_hop_boundaries():
    signal = np.concatenate(
        [
            np.zeros(641, dtype=np.float32),
            np.full(17000, 0.2, dtype=np.float32),
            np.zeros(5001, dtype=np.float32),
        ]
    )
    got = silence.trim_silence(signal)
    assert np.array_equal(got, signal[640:17920])
    assert 640 % silence.FRAME_SAMPLES == 0
    assert 17920 % silence.FRAME_SAMPLES == 0


def test_trim_silence_keeps_the_input_when_too_little_would_remain():
    signal = np.concatenate(
        [
            np.zeros(3200, dtype=np.float32),
            np.full(8000, 0.2, dtype=np.float32),
            np.zeros(3200, dtype=np.float32),
        ]
    )
    assert np.array_equal(silence.trim_silence(signal), signal)


def test_trim_silence_keeps_an_all_silence_input():
    signal = np.zeros(96000, dtype=np.float32)
    assert np.array_equal(silence.trim_silence(signal), signal)


def test_reference_clip_trims_before_tiling_to_six_seconds():
    speech = np.full(3 * 16000, 0.2, dtype=np.float32)
    signal = np.concatenate([np.zeros(2 * 16000, dtype=np.float32), speech, np.zeros(2 * 16000, dtype=np.float32)])

    got = enroll.reference_clip(signal)

    assert got.shape == (6 * 16000,)
    assert np.allclose(got, 0.2)
