"""Tests for the NumPy mel implementation used by Spark-TTS."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.spark_tts import mel  # noqa: E402

#: torchaudio's output for `default_rng(1234).standard_normal(96000) * 0.1`.
#: Noise exercises the full filterbank.
_GOLDEN_SUM = 3397.8916015625
_GOLDEN_MAX = 0.3124436140060425
_GOLDEN_CELLS = {
    (0, 0): 0.08807659894227982,
    (0, 150): 0.1566806584596634,
    (1, 1): 0.09200382232666016,
    (37, 42): 0.0639752522110939,
    (64, 0): 0.08108115196228027,
    (64, 300): 0.0594625361263752,
    (127, 150): 0.12104720622301102,
    (127, 300): 0.08309565484523773,
}


def _golden_signal():
    return (np.random.default_rng(1234).standard_normal(96000) * 0.1).astype(np.float32)


def test_matches_torchaudio_on_recorded_goldens():
    spectrogram = mel.mel_spectrogram(_golden_signal())
    assert spectrogram.shape == (128, 301)
    assert spectrogram.dtype == np.float32
    # Compare against torchaudio-derived reference values.
    assert spectrogram.sum() == pytest.approx(_GOLDEN_SUM, rel=1e-5)
    assert spectrogram.max() == pytest.approx(_GOLDEN_MAX, rel=1e-5)
    for (row, col), want in _GOLDEN_CELLS.items():
        assert spectrogram[row, col] == pytest.approx(want, rel=1e-4), f"mel[{row},{col}]"


def test_frame_count_follows_centred_framing():
    """Centered framing produces one frame per hop plus the initial frame."""
    for samples in (16000, 96000, 480000):
        assert mel.mel_spectrogram(np.zeros(samples, dtype=np.float32)).shape == (128, samples // 320 + 1)


def test_filterbank_is_slaney_normalised_and_ordered():
    bank = mel.mel_filterbank()
    assert bank.shape == (513, 128)
    assert np.isfinite(bank).all()
    assert (bank >= 0).all()
    # Filter peaks increase and Slaney normalization lowers wider filters.
    peaks = bank.argmax(axis=0)
    assert (np.diff(peaks) > 0).all()
    assert bank[:, 0].max() > bank[:, -1].max()


def test_hann_window_is_periodic_not_symmetric():
    """The window matches torch's periodic Hann convention."""
    window = mel._hann_periodic(640)
    assert window[0] == 0.0
    assert window[-1] != 0.0  # a symmetric window would end at zero
    assert np.allclose(window, np.hanning(641)[:-1])


def test_silence_is_silent_and_finite():
    spectrogram = mel.mel_spectrogram(np.zeros(96000, dtype=np.float32))
    assert np.isfinite(spectrogram).all()
    assert spectrogram.max() == 0.0
