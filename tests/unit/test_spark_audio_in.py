"""Decoding an upload, and the two transforms that move the enrolled voice.

Resampling and volume normalisation both change the mel, and the mel is what
the FSQ quantiser turns into the 32 integers that ARE the voice. So they are
correctness surface, not convenience, and are tested on signals whose right
answer is known analytically rather than on a fixture.

numpy arrives with onnxruntime, which base Orb does not install, so the whole
module skips without it rather than failing a plain `pytest`.
"""

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
    """The stdlib path is the only one guaranteed to exist, so it carries the
    formats a user is most likely to have. 24-bit is here because `audioop`,
    which used to widen it, was removed in Python 3.13."""
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
    """44.1 kHz is what a phone recording arrives at, and a naive decimation
    would fold everything above 8 kHz back into the speech band."""
    got = audio_in.decode(wav_bytes(tone(1.0, 44100), 44100), filename="clip.wav")
    assert abs(len(got) - 16000) <= 1
    expected = tone(len(got) / 16000, 16000)
    interior = slice(200, len(got) - 200)  # the kernel's edge taps read past the ends
    assert np.abs(got[interior] - expected[interior]).max() < 0.01


def test_resampling_a_tone_above_the_new_nyquist_attenuates_it():
    """The anti-aliasing half of the same property: 7 kHz survives a trip to
    16 kHz, 15 kHz must not come back as a 1 kHz whistle."""
    kept = audio_in.resample(tone(0.5, 44100, hz=7000), 44100, 16000)
    folded = audio_in.resample(tone(0.5, 44100, hz=15000), 44100, 16000)
    interior = slice(500, -500)
    assert np.abs(kept[interior]).max() > 0.3
    assert np.abs(folded[interior]).max() < 0.05


def test_resample_is_a_no_op_at_the_target_rate():
    signal = tone(0.1, 16000)
    assert audio_in.resample(signal, 16000, 16000) is not None
    assert np.array_equal(audio_in.resample(signal, 16000, 16000), signal)


def test_reference_window_tiles_a_short_clip_and_truncates_a_long_one():
    """Upstream REPEATS a clip shorter than six seconds rather than padding it
    with silence, and silence would be six seconds of a speaker who is not
    speaking."""
    short = tone(1.0, 16000)
    window = audio_in.reference_window(short)
    assert window.shape == (96000,)
    assert np.array_equal(window[:16000], short)
    assert np.array_equal(window[16000:32000], short)  # tiled, not zero-padded
    assert audio_in.reference_window(tone(30.0, 16000)).shape == (96000,)
    assert audio_in.reference_window(np.zeros(0, dtype=np.float32)).shape == (96000,)


def test_volume_normalize_lifts_a_quiet_recording_and_never_clips():
    quiet = tone(1.0, 16000, amplitude=0.01)
    loud = tone(1.0, 16000, amplitude=3.0)
    assert np.abs(audio_in.volume_normalize(quiet)).max() > np.abs(quiet).max()
    assert np.abs(audio_in.volume_normalize(loud)).max() <= 1.0


def test_volume_normalize_leaves_a_signal_with_too_little_content_alone():
    """Upstream's guard: fewer than ten samples above 0.01 is not enough to
    measure a level from, so the gain is not guessed."""
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
