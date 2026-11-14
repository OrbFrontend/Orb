"""Separate a clip's vocals with Hybrid Demucs exported as ONNX (scripts/export_vocal_separator.py)."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from .. import onnx_runtime
from . import catalog

if TYPE_CHECKING:
    import numpy as np

SAMPLE_RATE = 44100
#: The exported graph's fixed input: 10 s of stereo audio.
SEGMENT = 441000
_OVERLAP = SAMPLE_RATE
_NFFT = 4096
_HOP = _NFFT // 4


def _hann() -> np.ndarray:
    import numpy as np  # noqa: PLC0415 -- deferred; numpy arrives with onnxruntime

    return (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(_NFFT) / _NFFT)).astype(np.float64)


def stft(x: np.ndarray) -> np.ndarray:
    """torch.stft(normalized=True, center=True, reflect) of ``[channels, samples]``: ``[channels, NFFT/2+1, frames]``."""
    import numpy as np  # noqa: PLC0415 -- deferred; numpy arrives with onnxruntime

    padded = np.pad(x, ((0, 0), (_NFFT // 2, _NFFT // 2)), mode="reflect")
    frames = 1 + (padded.shape[1] - _NFFT) // _HOP
    index = np.arange(_NFFT)[None, :] + _HOP * np.arange(frames)[:, None]
    return np.fft.rfft(padded[:, index] * _hann(), axis=-1).transpose(0, 2, 1) / math.sqrt(_NFFT)


def istft(z: np.ndarray, length: int) -> np.ndarray:
    """torch.istft(normalized=True, center=True) of ``[channels, NFFT/2+1, frames]`` to *length* samples."""
    import numpy as np  # noqa: PLC0415 -- deferred; numpy arrives with onnxruntime

    window = _hann()
    frames = np.fft.irfft(z.transpose(0, 2, 1) * math.sqrt(_NFFT), n=_NFFT, axis=-1) * window
    count = frames.shape[1]
    total = _NFFT + _HOP * (count - 1)
    out = np.zeros((z.shape[0], total))
    envelope = np.zeros(total)
    for i in range(count):
        out[:, i * _HOP : i * _HOP + _NFFT] += frames[:, i]
        envelope[i * _HOP : i * _HOP + _NFFT] += window**2
    out /= np.where(envelope > 1e-11, envelope, 1.0)
    return out[:, _NFFT // 2 : _NFFT // 2 + length]


def _segment_vocals(session, segment: np.ndarray) -> np.ndarray:
    """The vocal stem of one normalized ``[2, SEGMENT]`` segment (HDemucs._spec / _magnitude / _mask / _ispec around the graph)."""
    import numpy as np  # noqa: PLC0415 -- deferred; numpy arrives with onnxruntime

    frames = math.ceil(SEGMENT / _HOP)
    pad = _HOP // 2 * 3
    z = stft(np.pad(segment, ((0, 0), (pad, pad + frames * _HOP - SEGMENT)), mode="reflect"))[:, :-1, 2 : 2 + frames]
    spec = np.stack([np.real(z), np.imag(z)], axis=1).reshape(1, -1, z.shape[1], frames).astype(np.float32)
    time, freq = session.run(None, {"wave": segment[None].astype(np.float32), "spec": spec})
    freq = freq[0].reshape(2, 2, freq.shape[2], frames)
    zout = np.pad(freq[:, 0] + 1j * freq[:, 1], ((0, 0), (0, 1), (2, 2)))
    spectral = istft(zout, _HOP * frames + 2 * pad)[:, pad : pad + SEGMENT]
    return time[0] + spectral


@onnx_runtime.using
def vocals(signal: np.ndarray) -> np.ndarray:
    """The vocals of a mono 44.1 kHz signal, mono, the same length."""
    import numpy as np  # noqa: PLC0415 -- deferred; numpy arrives with onnxruntime

    mono = np.asarray(signal, dtype=np.float64).reshape(-1)
    mean, std = float(mono.mean()), float(mono.std()) or 1.0
    stereo = np.stack([mono, mono]) - mean
    stereo /= std
    session = onnx_runtime.load(catalog.model_path())
    out = np.zeros_like(stereo)
    weight = np.zeros(mono.size)
    step = SEGMENT - _OVERLAP
    fade = np.linspace(0.0, 1.0, _OVERLAP)
    for start in range(0, max(1, mono.size - _OVERLAP), step):
        end = min(start + SEGMENT, mono.size)
        segment = np.zeros((2, SEGMENT))
        segment[:, : end - start] = stereo[:, start:end]
        w = np.ones(end - start)
        if start > 0:
            w[:_OVERLAP] = fade[: end - start]
        if end < mono.size:
            w[-_OVERLAP:] = fade[::-1]
        out[:, start:end] += _segment_vocals(session, segment)[:, : end - start] * w
        weight[start:end] += w
        if end == mono.size:
            break
    return (out.mean(axis=0) / np.where(weight > 0, weight, 1.0) * std + mean).astype(np.float32)


def release() -> None:
    """Release the separator's session."""
    onnx_runtime.release(catalog.model_path())
