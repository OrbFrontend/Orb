"""Compute the mel spectrogram used by Spark-TTS speaker enrollment."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy arrives with onnxruntime; base Orb must import this module without it
    import numpy as np

# BiCodec/config.yaml -> audio_tokenizer.mel_params, verbatim.
SAMPLE_RATE = 16000
N_FFT = 1024
WIN_LENGTH = 640
HOP_LENGTH = 320
MEL_FMIN = 10.0
MEL_FMAX = SAMPLE_RATE / 2  # config says null, which torchaudio reads as Nyquist
NUM_MELS = 128


def _hz_to_mel(freq):
    """Slaney's mel scale: linear below 1 kHz, logarithmic above."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    freq = np.asarray(freq, dtype=np.float64)
    # Keep the logarithm defined for values in the unused branch of np.where.
    safe = np.maximum(freq, min_log_hz)
    return np.where(
        freq >= min_log_hz,
        min_log_mel + np.log(safe / min_log_hz) / logstep,
        freq / f_sp,
    )


def _mel_to_hz(mels):
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    mels = np.asarray(mels, dtype=np.float64)
    return np.where(
        mels >= min_log_mel,
        min_log_hz * np.exp(logstep * (mels - min_log_mel)),
        f_sp * mels,
    )


def mel_filterbank() -> np.ndarray:
    """Return the Slaney-normalized mel filterbank."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    n_freqs = N_FFT // 2 + 1
    all_freqs = np.linspace(0, SAMPLE_RATE // 2, n_freqs, dtype=np.float64)
    m_pts = np.linspace(_hz_to_mel(MEL_FMIN), _hz_to_mel(MEL_FMAX), NUM_MELS + 2)
    f_pts = _mel_to_hz(m_pts)
    f_diff = np.diff(f_pts)  # (n_mels + 1,)
    slopes = f_pts[None, :] - all_freqs[:, None]  # (n_freqs, n_mels + 2)
    down = -slopes[:, :-2] / f_diff[None, :-1]
    up = slopes[:, 2:] / f_diff[None, 1:]
    fb = np.maximum(0.0, np.minimum(down, up))
    fb *= (2.0 / (f_pts[2 : NUM_MELS + 2] - f_pts[:NUM_MELS]))[None, :]  # norm="slaney"
    return fb


def _hann_periodic(length: int) -> np.ndarray:
    """Return the periodic Hann window used by torch."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    n = np.arange(length, dtype=np.float64)
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * n / length)


def _stft_magnitude(wav: np.ndarray) -> np.ndarray:
    """``(n_freqs, frames)`` magnitudes, matching ``torch.stft``'s framing."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    # Match torch.stft's centered, reflect-padded framing.
    padded = np.pad(np.asarray(wav, dtype=np.float64), N_FFT // 2, mode="reflect")
    # Center the shorter analysis window inside each n_fft frame.
    window = np.zeros(N_FFT, dtype=np.float64)
    left = (N_FFT - WIN_LENGTH) // 2
    window[left : left + WIN_LENGTH] = _hann_periodic(WIN_LENGTH)
    frames = 1 + (len(padded) - N_FFT) // HOP_LENGTH
    if frames <= 0:
        return np.zeros((N_FFT // 2 + 1, 0), dtype=np.float64)
    # Use a strided view instead of a Python loop.
    shape = (frames, N_FFT)
    strides = (padded.strides[0] * HOP_LENGTH, padded.strides[0])
    blocks = np.lib.stride_tricks.as_strided(padded, shape=shape, strides=strides)
    return np.abs(np.fft.rfft(blocks * window, n=N_FFT, axis=1)).T


def mel_spectrogram(wav: np.ndarray) -> np.ndarray:
    """``(n_mels, frames)`` float32 mel magnitudes for 16 kHz mono *wav*."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    spec = _stft_magnitude(wav)  # power=1: magnitude, not energy
    return (mel_filterbank().T @ spec).astype(np.float32)


__all__ = ["HOP_LENGTH", "NUM_MELS", "SAMPLE_RATE", "mel_filterbank", "mel_spectrogram"]
