"""Compute Whisper's log-mel input features in NumPy."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy arrives with onnxruntime; base Orb must import this module without it
    import numpy as np

# Whisper's front end, fixed by the architecture rather than by a checkpoint:
# every size reads 30 s windows of 16 kHz audio with a 25 ms / 10 ms STFT. The
# mel count does vary (80, or 128 for large-v3) and comes from the config.
SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
CHUNK_SAMPLES = 30 * SAMPLE_RATE
N_FRAMES = CHUNK_SAMPLES // HOP_LENGTH


def _hz_to_mel(freq):
    """Slaney's mel scale: linear below 1 kHz, logarithmic above."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    freq = np.asarray(freq, dtype=np.float64)
    safe = np.maximum(freq, min_log_hz)
    return np.where(freq >= min_log_hz, min_log_mel + np.log(safe / min_log_hz) / logstep, freq / f_sp)


def _mel_to_hz(mels):
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    mels = np.asarray(mels, dtype=np.float64)
    return np.where(mels >= min_log_mel, min_log_hz * np.exp(logstep * (mels - min_log_mel)), f_sp * mels)


def mel_filters(n_mels: int) -> np.ndarray:
    """``(n_freqs, n_mels)`` Slaney-normalized filterbank spanning 0–8 kHz."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    n_freqs = N_FFT // 2 + 1
    fft_freqs = np.linspace(0, SAMPLE_RATE // 2, n_freqs, dtype=np.float64)
    f_pts = _mel_to_hz(np.linspace(_hz_to_mel(0.0), _hz_to_mel(SAMPLE_RATE / 2), n_mels + 2))
    f_diff = np.diff(f_pts)
    slopes = f_pts[None, :] - fft_freqs[:, None]
    down = -slopes[:, :-2] / f_diff[None, :-1]
    up = slopes[:, 2:] / f_diff[None, 1:]
    fb = np.maximum(0.0, np.minimum(down, up))
    return fb * (2.0 / (f_pts[2 : n_mels + 2] - f_pts[:n_mels]))[None, :]


def log_mel(wav: np.ndarray, n_mels: int) -> np.ndarray:
    """``(n_mels, 3000)`` float32 features for up to 30 s of 16 kHz mono audio.

    Longer input is truncated and shorter input zero-padded, the way Whisper's
    feature extractor fills its fixed window.
    """
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    audio = np.zeros(CHUNK_SAMPLES, dtype=np.float64)
    source = np.asarray(wav, dtype=np.float64).reshape(-1)[:CHUNK_SAMPLES]
    audio[: source.size] = source
    padded = np.pad(audio, N_FFT // 2, mode="reflect")
    frames = 1 + (padded.size - N_FFT) // HOP_LENGTH
    blocks = np.lib.stride_tricks.as_strided(
        padded,
        shape=(frames, N_FFT),
        strides=(padded.strides[0] * HOP_LENGTH, padded.strides[0]),
    )
    window = 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(N_FFT) / N_FFT)  # periodic Hann
    power = np.abs(np.fft.rfft(blocks * window, n=N_FFT, axis=1)) ** 2
    mel = np.maximum(1e-10, mel_filters(n_mels).T @ power.T)
    spec = np.log10(mel)[:, :N_FRAMES]  # the centred STFT yields one frame past the window
    spec = np.maximum(spec, spec.max() - 8.0)
    return ((spec + 4.0) / 4.0).astype(np.float32)


__all__ = ["CHUNK_SAMPLES", "N_FRAMES", "SAMPLE_RATE", "log_mel", "mel_filters"]
