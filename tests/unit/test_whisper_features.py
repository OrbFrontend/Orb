"""Whisper's log-mel front end, pinned to transformers' WhisperFeatureExtractor."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")

from backend.inference.local_models.whisper import features  # noqa: E402

# transformers 4.46.2 WhisperFeatureExtractor(feature_size=n) on _signal():
# ``{n_mels: ([(mel, frame, value), ...], mean of the first 250 frames, min, max)}``
_GOLDEN = {
    80: ([(0, 0, 1.04383), (5, 40, 0.27258), (40, 120, 0.26594), (79, 249, 0.3136), (20, 2999, -0.66024)], 0.30223, -0.66024, 1.33976),
    128: ([(0, 0, 0.96807), (5, 40, 0.13548), (64, 120, 0.27106), (127, 249, 0.27352), (20, 2999, -0.61056)], 0.28211, -0.61056, 1.38944),
}  # fmt: skip


def _signal() -> np.ndarray:
    """A 2.5 s chirp over faint noise: every band carries something."""
    t = np.arange(int(16000 * 2.5)) / 16000
    chirp = 0.3 * np.sin(2 * np.pi * (200 + 900 * t) * t)
    return (chirp + 0.02 * np.random.default_rng(7).standard_normal(t.size)).astype(np.float32)


@pytest.mark.parametrize("n_mels", sorted(_GOLDEN))
def test_matches_transformers(n_mels):
    points, mean, low, high = _GOLDEN[n_mels]
    got = features.log_mel(_signal(), n_mels)
    assert got.shape == (n_mels, features.N_FRAMES)
    assert got.dtype == np.float32
    for mel, frame, value in points:
        assert got[mel, frame] == pytest.approx(value, abs=1e-4), (mel, frame)
    assert got[:, :250].mean() == pytest.approx(mean, abs=1e-4)
    assert got.min() == pytest.approx(low, abs=1e-4)
    assert got.max() == pytest.approx(high, abs=1e-4)


def test_input_past_thirty_seconds_is_cut_and_short_input_is_padded():
    long = np.ones(features.CHUNK_SAMPLES + 16000, dtype=np.float32) * 0.1
    assert features.log_mel(long, 80).shape == (80, features.N_FRAMES)
    assert features.log_mel(np.zeros(10, dtype=np.float32), 80).shape == (80, features.N_FRAMES)


def test_the_dynamic_range_is_clamped_to_eight_decades():
    """Whisper floors everything eight decades (in log10) under the loudest bin."""
    got = features.log_mel(_signal(), 80)
    assert got.max() - got.min() <= 8.0 / 4.0 + 1e-6
