"""Tests for Spark-TTS audio decoding and preparation."""

from __future__ import annotations

import io
import wave

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.spark_tts import audio_in  # noqa: E402


def wav_bytes(signal, rate: int, width: int = 2, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(width)
        handle.setframerate(rate)
        clipped = np.clip(signal, -1, 1)
        if width == 1:
            frames = (clipped * 127 + 128).astype(np.uint8).tobytes()
        elif width == 3:
            frames = (clipped * 8388607).astype("<i4").view(np.uint8).reshape(-1, 4)[:, :3].tobytes()
        else:
            scale = {2: 32767, 4: 2147483647}[width]
            frames = (clipped * scale).astype(f"<i{width}").tobytes()
        handle.writeframes(frames)
    return buffer.getvalue()


def tone(seconds: float, rate: int, hz: float = 440.0, amplitude: float = 0.5):
    return (amplitude * np.sin(2 * np.pi * hz * np.arange(int(seconds * rate)) / rate)).astype(np.float32)


@pytest.mark.parametrize("width", [1, 2, 3, 4])
def test_every_pcm_wav_width_decodes_without_soundfile(width):
    """The stdlib reader handles supported PCM widths."""
    signal = tone(0.5, 16000)
    got = audio_in.decode(wav_bytes(signal, 16000, width), filename="clip.wav")
    tolerance = 0.02 if width == 1 else 1e-3  # 8-bit has 8 bits
    assert got.shape == signal.shape
    assert np.abs(got - signal).max() < tolerance


def test_stereo_is_mixed_to_mono_rather_than_taking_one_side():
    left, right = tone(0.25, 16000), tone(0.25, 16000, amplitude=0.1)
    interleaved = np.stack([left, right], axis=1).reshape(-1)
    got = audio_in.decode(wav_bytes(interleaved, 16000, channels=2), filename="clip.wav")
    assert np.abs(got - (left + right) / 2).max() < 1e-3


def test_resampling_preserves_a_tone_rather_than_aliasing_it():
    """Resampling preserves a tone without aliasing."""
    got = audio_in.decode(wav_bytes(tone(1.0, 44100), 44100), filename="clip.wav")
    assert abs(len(got) - 16000) <= 1
    expected = tone(len(got) / 16000, 16000)
    interior = slice(200, len(got) - 200)  # the kernel's edge taps read past the ends
    assert np.abs(got[interior] - expected[interior]).max() < 0.01


def test_resampling_a_tone_above_the_new_nyquist_attenuates_it():
    """Frequencies above the target Nyquist are attenuated."""
    kept = audio_in.resample(tone(0.5, 44100, hz=7000), 44100, 16000)
    folded = audio_in.resample(tone(0.5, 44100, hz=15000), 44100, 16000)
    interior = slice(500, -500)
    assert np.abs(kept[interior]).max() > 0.3
    assert np.abs(folded[interior]).max() < 0.05


def test_resample_is_a_no_op_at_the_target_rate():
    signal = tone(0.1, 16000)
    assert audio_in.resample(signal, 16000, 16000) is not None
    assert np.array_equal(audio_in.resample(signal, 16000, 16000), signal)


def test_reference_signal_tiles_a_short_clip_to_exactly_six_seconds():
    """Short clips are repeated to six seconds without padding."""
    short = tone(1.0, 16000)
    signal = audio_in.reference_signal(short)
    assert signal.shape == (96000,)
    assert np.array_equal(signal[:16000], short)
    assert np.array_equal(signal[16000:32000], short)  # tiled, not zero-padded
    assert audio_in.reference_signal(tone(0.0629, 16000)).shape == (96000,)  # 1006 samples: repeats overshoot
    assert audio_in.reference_signal(np.zeros(0, dtype=np.float32)).shape == (96000,)


def test_reference_signal_keeps_a_long_clip_whole():
    """Long clips are retained and trimmed to a complete mel hop."""
    long = tone(30.0, 16000)
    assert np.array_equal(audio_in.reference_signal(long), long)
    assert audio_in.reference_signal(tone(10.01, 16000)).shape == (160160 // 320 * 320,)


def test_volume_normalize_lifts_a_quiet_recording_and_never_clips():
    quiet = tone(1.0, 16000, amplitude=0.01)
    loud = tone(1.0, 16000, amplitude=3.0)
    assert np.abs(audio_in.volume_normalize(quiet)).max() > np.abs(quiet).max()
    assert np.abs(audio_in.volume_normalize(loud)).max() <= 1.0


def test_volume_normalize_leaves_a_signal_with_too_little_content_alone():
    """Sparse signals are not gain-adjusted."""
    sparse = np.zeros(16000, dtype=np.float32)
    sparse[:5] = 0.5
    assert np.array_equal(audio_in.volume_normalize(sparse), sparse)


def test_an_unreadable_upload_names_what_would_have_read_it():
    with pytest.raises(audio_in.UnsupportedAudio) as excinfo:
        audio_in.decode(b"\x00\x01\x02\x03 not audio", filename="voice.mp3")
    assert "MP3" in str(excinfo.value)
    assert "ffmpeg" in str(excinfo.value)
    with pytest.raises(audio_in.UnsupportedAudio):
        audio_in.decode(b"", filename="voice.wav")
