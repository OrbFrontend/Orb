"""Tests for Spark-TTS waveform decoding."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.spark_tts import codec, silence  # noqa: E402


class _DecoderSession:
    def __init__(self, waveform):
        self.waveform = waveform

    def run(self, outputs, inputs):
        assert outputs == ["audio"]
        assert inputs["semantic_tokens"].shape == (1, 3)
        assert inputs["global_tokens"].shape == (1, 1, 32)
        return [self.waveform[None, None, :]]


def test_decode_trims_edges_keeps_an_interior_pause_and_ends_on_silence(monkeypatch):
    speech_a = np.full(16000, 0.2, dtype=np.float32)
    pause = np.zeros(9600, dtype=np.float32)
    speech_b = np.full(16000, -0.2, dtype=np.float32)
    waveform = np.concatenate([np.zeros(640, dtype=np.float32), speech_a, pause, speech_b, np.zeros(960, dtype=np.float32)])
    monkeypatch.setattr(codec.onnx_runtime, "load", lambda _: _DecoderSession(waveform))

    pcm = codec.decode([1, 2, 3], [0] * 32)
    samples = np.frombuffer(pcm, dtype="<i2")

    fade = silence.FADE_MS * codec.SAMPLE_RATE // 1000
    tail = silence.RELEASE_PAD_MS * codec.SAMPLE_RATE // 1000
    assert samples.size == speech_a.size + pause.size + speech_b.size + tail
    assert np.all(samples[fade : speech_a.size] > 0)
    assert np.all(samples[speech_a.size : speech_a.size + pause.size] == 0)
    assert np.all(samples[speech_a.size + pause.size : -tail] < 0)
    # The line must not stop dead on its last audible sample.
    assert np.all(samples[-tail:] == 0)


def test_decode_can_preserve_the_untrimmed_waveform(monkeypatch):
    waveform = np.concatenate(
        [np.zeros(640, dtype=np.float32), np.full(16000, 0.2, dtype=np.float32), np.zeros(960, dtype=np.float32)]
    )
    monkeypatch.setattr(codec.onnx_runtime, "load", lambda _: _DecoderSession(waveform))

    pcm = codec.decode([1, 2, 3], [0] * 32, trim=False)

    assert len(pcm) == waveform.size * 2
