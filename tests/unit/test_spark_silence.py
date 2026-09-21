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


def test_trim_silence_edges_keeps_a_release_quieter_than_the_speech_threshold():
    """The decay of the final word sits below the speech threshold, and is speech."""
    speech = np.full(16000, 0.2, dtype=np.float32)
    decay = np.concatenate([np.full(320, level, dtype=np.float32) for level in (0.008, 0.004, 0.002)])
    signal = np.concatenate([np.zeros(640, dtype=np.float32), speech, decay, np.zeros(9600, dtype=np.float32)])

    got = silence.trim_silence_edges(signal)

    pad = silence.RELEASE_PAD_MS * silence.SAMPLE_RATE // 1000
    assert got.size == speech.size + decay.size + pad
    assert np.allclose(got[speech.size : speech.size + decay.size], decay)
    assert not got[speech.size + decay.size :].any()


def test_trim_silence_edges_stops_following_a_floor_after_release_ms():
    """A hiss above the decay floor must not defeat the trim outright."""
    speech = np.full(16000, 0.2, dtype=np.float32)
    hiss = np.full(16000, 0.005, dtype=np.float32)
    signal = np.concatenate([speech, hiss])

    got = silence.trim_silence_edges(signal)

    followed = silence.RELEASE_MS * silence.SAMPLE_RATE // 1000
    pad = silence.RELEASE_PAD_MS * silence.SAMPLE_RATE // 1000
    assert got.size == speech.size + followed + pad


def test_trim_silence_edges_ramps_a_cut_that_lands_on_signal():
    """A waveform that runs out mid-word ends on a ramp, not on a step."""
    signal = np.full(16000, 0.2, dtype=np.float32)

    got = silence.trim_silence_edges(signal)

    fade = silence.FADE_MS * silence.SAMPLE_RATE // 1000
    assert got.size == signal.size
    assert abs(float(got[0])) < 1e-6
    assert abs(float(got[-1])) < 1e-6
    assert np.allclose(got[fade:-fade], 0.2)


def test_trim_silence_edges_shapes_a_clip_shorter_than_a_second():
    """A one-word line gets the same edges as a sentence."""
    signal = np.concatenate(
        [np.zeros(640, dtype=np.float32), np.full(4800, 0.2, dtype=np.float32), np.zeros(320, dtype=np.float32)]
    )

    got = silence.trim_silence_edges(signal)

    assert got.size == 4800 + 320  # lead-in dropped, the short tail kept


def test_trim_silence_keeps_its_speech_edges_for_enrollment():
    """Enrollment feeds a speaker encoder, not a listener: no release, no pad."""
    speech = np.full(16000, 0.2, dtype=np.float32)
    signal = np.concatenate(
        [np.zeros(640, dtype=np.float32), speech, np.full(320, 0.005, dtype=np.float32), np.zeros(960, dtype=np.float32)]
    )

    assert np.array_equal(silence.trim_silence(signal), speech)


def test_pad_tail_tops_up_a_clip_that_ends_on_speech():
    signal = np.full(4800, 0.2, dtype=np.float32)

    got = silence.pad_tail(signal)

    pad = silence.RELEASE_PAD_MS * silence.SAMPLE_RATE // 1000
    assert got.size == signal.size + pad
    assert np.array_equal(got[: signal.size], signal)
    assert not got[signal.size :].any()


def test_pad_tail_leaves_a_clip_that_already_ends_quietly():
    pad = silence.RELEASE_PAD_MS * silence.SAMPLE_RATE // 1000
    signal = np.concatenate([np.full(4800, 0.2, dtype=np.float32), np.zeros(pad, dtype=np.float32)])

    assert np.array_equal(silence.pad_tail(signal), signal)


def test_reference_clip_trims_before_tiling_to_six_seconds():
    speech = np.full(3 * 16000, 0.2, dtype=np.float32)
    signal = np.concatenate([np.zeros(2 * 16000, dtype=np.float32), speech, np.zeros(2 * 16000, dtype=np.float32)])

    got = enroll.reference_clip(signal)

    assert got.shape == (6 * 16000,)
    assert np.allclose(got, 0.2)
