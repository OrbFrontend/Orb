"""The numpy mel, pinned to the torchaudio one it replaces.

Upstream computes this spectrogram with ``torchaudio.transforms.MelSpectrogram``.
Reimplementing it in numpy is what removes 328 MB of torch from the install, and
the reimplementation is only as good as its agreement with the original: the
speaker encoder quantises this mel into 32 FSQ codes, so a wrong window shape, a
symmetric Hann instead of a periodic one, or HTK mel instead of Slaney does not
raise — it enrolls a voice that is subtly not the uploaded one.

The goldens below were produced by torchaudio 2.5.1 with exactly the parameters
in ``BiCodec/config.yaml``::

    MelSpectrogram(16000, n_fft=1024, win_length=640, hop_length=320,
                   f_min=10, f_max=None, n_mels=128,
                   power=1, norm="slaney", mel_scale="slaney")

``tests/unit/test_spark_enroll.py`` closes the loop on real audio: the same
implementation reproduces, bit for bit, the 32 tokens torch derives for the
reference clip.
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.spark_tts import mel  # noqa: E402

#: torchaudio's output for `default_rng(1234).standard_normal(96000) * 0.1`.
#: Noise rather than a tone on purpose: a tone excites a handful of bins and
#: would pass against a filterbank that is wrong almost everywhere.
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
    # float32 noise, not agreement to a tolerance chosen to make it pass: the
    # measured relative error against torchaudio is ~2e-6 on real audio.
    assert spectrogram.sum() == pytest.approx(_GOLDEN_SUM, rel=1e-5)
    assert spectrogram.max() == pytest.approx(_GOLDEN_MAX, rel=1e-5)
    for (row, col), want in _GOLDEN_CELLS.items():
        assert spectrogram[row, col] == pytest.approx(want, rel=1e-4), f"mel[{row},{col}]"


def test_frame_count_follows_centred_framing():
    """``center=True`` reflects n_fft//2 off each end, so the count is
    ``len // hop + 1``. Framing the signal without that padding would drop the
    first 32 ms of every enrollment and shift all the rest."""
    for samples in (16000, 96000, 480000):
        assert mel.mel_spectrogram(np.zeros(samples, dtype=np.float32)).shape == (128, samples // 320 + 1)


def test_filterbank_is_slaney_normalised_and_ordered():
    bank = mel.mel_filterbank()
    assert bank.shape == (513, 128)
    assert np.isfinite(bank).all()
    assert (bank >= 0).all()
    # Each triangle peaks at a higher frequency than the last, and Slaney
    # normalisation makes the wider high filters shorter rather than louder.
    peaks = bank.argmax(axis=0)
    assert (np.diff(peaks) > 0).all()
    assert bank[:, 0].max() > bank[:, -1].max()


def test_hann_window_is_periodic_not_symmetric():
    """numpy's ``hanning`` is the symmetric one; torch's default is periodic,
    and they differ at every sample."""
    window = mel._hann_periodic(640)
    assert window[0] == 0.0
    assert window[-1] != 0.0  # a symmetric window would end at zero
    assert np.allclose(window, np.hanning(641)[:-1])


def test_silence_is_silent_and_finite():
    spectrogram = mel.mel_spectrogram(np.zeros(96000, dtype=np.float32))
    assert np.isfinite(spectrogram).all()
    assert spectrogram.max() == 0.0
