"""Dependency-free silence detection and trimming for Spark-TTS audio."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy is an optional runtime dependency
    import numpy as np

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320

#: RMS below which a word's release has finished. Speech detection runs at a far
#: higher threshold, and cutting a synthesized clip there severs the decay of
#: the final word while it is still audible — which is what made generated lines
#: sound chopped off the moment the last word landed. Spark's decoder reaches
#: this floor within about 80 ms of its last speech frame.
RELEASE_THRESHOLD = 0.001

#: How far an edge may follow a decay past the last speech frame. Without a cap,
#: a clip whose noise floor sits above ``RELEASE_THRESHOLD`` would not be
#: trimmed at all.
RELEASE_MS = 200

#: Quiet audio kept after the decay, so a clip does not end the instant it goes
#: silent. Nothing is invented: the pad is the decoder's own trailing silence,
#: and it is shorter when the waveform holds less.
RELEASE_PAD_MS = 120

#: Ramp applied at each cut. The edges land in near-silence once the decay is
#: kept, but a waveform that runs out mid-decay would otherwise end on a step.
FADE_MS = 5


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


def _frames(ms: float) -> int:
    """Whole 20 ms frames in *ms* milliseconds."""
    return int(ms * SAMPLE_RATE / 1000) // FRAME_SAMPLES


def _release_bounds(rms: np.ndarray, first: int, last: int, threshold: float, limit: int) -> tuple[int, int]:
    """Widen the speech frames ``[first, last]`` across the attack and decay."""
    if threshold <= 0 or limit <= 0:
        return first, last
    start, floor = first, max(first - limit, 0)
    while start > floor and rms[start - 1] > threshold:
        start -= 1
    end, ceiling = last, min(last + limit, rms.size - 1)
    while end < ceiling and rms[end + 1] > threshold:
        end += 1
    return start, end


def _fade_edges(audio: np.ndarray, samples: int) -> np.ndarray:
    """Ramp both ends so a cut that lands on signal does not click."""
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    length = min(samples, audio.size // 2)
    if length <= 0:
        return audio
    ramp = ((1.0 - np.cos(np.linspace(0.0, np.pi, length))) / 2.0).astype(np.float32)
    faded = audio.copy()
    faded[:length] *= ramp
    faded[audio.size - length :] *= ramp[::-1]
    return faded


def _trim(
    wav: np.ndarray,
    *,
    threshold: float,
    min_silence_ms: int,
    keep_ms: int,
    min_keep_samples: int,
    clamp_interior: bool,
    release_threshold: float = 0.0,
    release_ms: int = 0,
    pad_ms: int = 0,
    fade_ms: int = 0,
) -> np.ndarray:
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    if min_silence_ms < 0 or keep_ms < 0 or min_keep_samples < 0:
        raise ValueError("silence durations and minimum length must not be negative")
    if threshold < 0:
        raise ValueError("threshold must not be negative")
    if release_threshold < 0 or release_ms < 0 or pad_ms < 0 or fade_ms < 0:
        raise ValueError("release settings must not be negative")
    audio = np.ascontiguousarray(np.asarray(wav, dtype=np.float32).reshape(-1))
    # One pass of frame energy serves both the speech mask and the decay floor.
    rms = frame_rms(audio, FRAME_SAMPLES, FRAME_SAMPLES)
    mask = rms > threshold
    spoken = np.flatnonzero(mask)
    if spoken.size == 0:
        return audio

    first = int(spoken[0])
    last = int(spoken[-1])
    # Interior clamping still reasons about speech frames; only the cuts move.
    edge_first, edge_last = _release_bounds(rms, first, last, release_threshold, _frames(release_ms))
    edge_start = edge_first * FRAME_SAMPLES
    edge_end = min((edge_last + 1) * FRAME_SAMPLES + int(pad_ms * SAMPLE_RATE / 1000), audio.size)
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
    if fade_ms:
        trimmed = _fade_edges(np.asarray(trimmed, dtype=np.float32), int(fade_ms * SAMPLE_RATE / 1000))
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
    # No floor: a synthesized line is as long as it is, and "Wait." deserves the
    # same edges as a sentence. The release-aware cut is conservative enough on
    # its own — it only ever removes audio that sits below the decay floor.
    min_keep_samples: int = 0,
    release_threshold: float = RELEASE_THRESHOLD,
    release_ms: int = RELEASE_MS,
    pad_ms: int = RELEASE_PAD_MS,
    fade_ms: int = FADE_MS,
) -> np.ndarray:
    """Remove dead air at the edges, keeping each word's attack and release.

    Pauses inside the speech are left alone. The edges are cut at the decay
    floor rather than at the last speech frame, and a short pad of the
    waveform's own trailing silence is kept, so a synthesized line ends the way
    it was spoken instead of stopping dead on the last audible sample.
    """
    return _trim(
        wav,
        threshold=threshold,
        min_silence_ms=0,
        keep_ms=0,
        min_keep_samples=min_keep_samples,
        clamp_interior=False,
        release_threshold=release_threshold,
        release_ms=release_ms,
        pad_ms=pad_ms,
        fade_ms=fade_ms,
    )


def pad_tail(wav: np.ndarray, *, ms: int = RELEASE_PAD_MS, threshold: float = RELEASE_THRESHOLD) -> np.ndarray:
    """Append silence so a clip ends with at least *ms* of quiet.

    Spark's decoder normally renders its own trailing silence and the trim keeps
    it, leaving this a no-op. A short line is the exception: the decoder can
    stop on the last audible sample, which is heard as the voice being cut off
    mid-breath.
    """
    import numpy as np  # noqa: PLC0415 — deferred; numpy arrives with onnxruntime

    if ms < 0 or threshold < 0:
        raise ValueError("tail length and threshold must not be negative")
    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return audio
    loud = np.flatnonzero(frame_rms(audio, FRAME_SAMPLES, FRAME_SAMPLES) > threshold)
    quiet = audio.size - (int(loud[-1]) + 1) * FRAME_SAMPLES if loud.size else audio.size
    missing = int(ms * SAMPLE_RATE / 1000) - max(quiet, 0)
    if missing <= 0:
        return audio
    return np.concatenate([audio, np.zeros(missing, dtype=np.float32)])


__all__ = [
    "FADE_MS",
    "FRAME_SAMPLES",
    "RELEASE_MS",
    "RELEASE_PAD_MS",
    "RELEASE_THRESHOLD",
    "SAMPLE_RATE",
    "frame_rms",
    "pad_tail",
    "speech_mask",
    "trim_silence",
    "trim_silence_edges",
]
