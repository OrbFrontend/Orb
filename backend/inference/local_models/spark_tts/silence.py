"""Dependency-free silence detection and trimming for Spark-TTS audio."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy is an optional runtime dependency
    import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320


def frame_rms(wav: np.ndarray, frame: int, hop: int) -> np.ndarray:
    """Return the RMS energy of each frame, including a partial final frame."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    if frame <= 0 or hop <= 0:
        raise ValueError("frame and hop must be positive")
    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return np.empty(0, dtype=np.float32)
    starts = np.arange(0, audio.size, hop)
    rms = np.fromiter(
        (np.sqrt(np.mean(np.square(audio[start : start + frame], dtype=np.float64))) for start in starts),
        dtype=np.float64,
        count=starts.size,
    )
    return rms.astype(np.float32)


def speech_mask(wav: np.ndarray, threshold: float = 0.01) -> np.ndarray:
    """Mark 20 ms mel-hop frames whose RMS exceeds ``threshold``."""
    if threshold < 0:
        raise ValueError("threshold must not be negative")
    return frame_rms(wav, FRAME_SAMPLES, FRAME_SAMPLES) > threshold


def _trim(
    wav: np.ndarray,
    *,
    threshold: float,
    min_silence_ms: int,
    keep_ms: int,
    min_keep_samples: int,
    clamp_interior: bool,
) -> np.ndarray:
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    if min_silence_ms < 0 or keep_ms < 0 or min_keep_samples < 0:
        raise ValueError("silence durations and minimum length must not be negative")
    audio = np.ascontiguousarray(np.asarray(wav, dtype=np.float32).reshape(-1))
    mask = speech_mask(audio, threshold)
    spoken = np.flatnonzero(mask)
    if spoken.size == 0:
        return audio

    first = int(spoken[0])
    last = int(spoken[-1])
    edge_start = first * FRAME_SAMPLES
    edge_end = min((last + 1) * FRAME_SAMPLES, audio.size)
    parts: list[np.ndarray] = []
    cursor = edge_start

    if clamp_interior:
        minimum_silence_samples = min_silence_ms * SAMPLE_RATE / 1000
        keep_frames = int(keep_ms * SAMPLE_RATE / 1000) // FRAME_SAMPLES
        frame_index = first + 1
        while frame_index < last:
            if mask[frame_index]:
                frame_index += 1
                continue
            run_start = frame_index
            while frame_index < last and not mask[frame_index]:
                frame_index += 1
            run_end = frame_index
            run_frames = run_end - run_start
            if run_frames * FRAME_SAMPLES <= minimum_silence_samples or run_frames <= keep_frames:
                continue
            left_kept = keep_frames // 2
            right_kept = keep_frames - left_kept
            parts.append(audio[cursor : (run_start + left_kept) * FRAME_SAMPLES])
            cursor = (run_end - right_kept) * FRAME_SAMPLES

    parts.append(audio[cursor:edge_end])
    trimmed = parts[0] if len(parts) == 1 else np.concatenate(parts)
    if trimmed.size < min_keep_samples:
        return audio
    return np.ascontiguousarray(trimmed, dtype=np.float32)


def trim_silence(
    wav: np.ndarray,
    *,
    threshold: float = 0.01,
    min_silence_ms: int = 300,
    keep_ms: int = 200,
    min_keep_samples: int = 16000,
) -> np.ndarray:
    """Remove silent edges and clamp long interior silence runs."""
    return _trim(
        wav,
        threshold=threshold,
        min_silence_ms=min_silence_ms,
        keep_ms=keep_ms,
        min_keep_samples=min_keep_samples,
        clamp_interior=True,
    )


def trim_silence_edges(
    wav: np.ndarray,
    *,
    threshold: float = 0.01,
    min_keep_samples: int = 16000,
) -> np.ndarray:
    """Remove silent edges without changing pauses inside speech."""
    return _trim(
        wav,
        threshold=threshold,
        min_silence_ms=0,
        keep_ms=0,
        min_keep_samples=min_keep_samples,
        clamp_interior=False,
    )


__all__ = [
    "FRAME_SAMPLES",
    "SAMPLE_RATE",
    "frame_rms",
    "speech_mask",
    "trim_silence",
    "trim_silence_edges",
]
