"""Compute Whisper's log-mel input features in NumPy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..mel import hz_to_mel, mel_to_hz

if TYPE_CHECKING:  # numpy arrives with onnxruntime; base Orb must import this module without it
    import numpy as np

# Whisper's fixed 30 s, 16 kHz, 25 ms / 10 ms front end.
SAMPLE_RATE = 16000
N_FFT = 400
HOP_LENGTH = 160
CHUNK_SAMPLES = 30 * SAMPLE_RATE
N_FRAMES = CHUNK_SAMPLES // HOP_LENGTH


def mel_filters(n_mels: int) -> np.ndarray:
    """``(n_freqs, n_mels)`` Slaney-normalized filterbank spanning 0–8 kHz."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    n_freqs = N_FFT // 2 + 1
    fft_freqs = np.linspace(0, SAMPLE_RATE // 2, n_freqs, dtype=np.float64)
    f_pts = mel_to_hz(np.linspace(hz_to_mel(0.0), hz_to_mel(SAMPLE_RATE / 2), n_mels + 2))
    f_diff = np.diff(f_pts)
    slopes = f_pts[None, :] - fft_freqs[:, None]
    down = -slopes[:, :-2] / f_diff[None, :-1]
    up = slopes[:, 2:] / f_diff[None, 1:]
    fb = np.maximum(0.0, np.minimum(down, up))
    return fb * (2.0 / (f_pts[2 : n_mels + 2] - f_pts[:n_mels]))[None, :]


def log_mel(wav: np.ndarray, n_mels: int) -> np.ndarray:
    """Return Whisper log-mel features for a fixed 30 s window."""
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
