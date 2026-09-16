"""Decode and prepare uploaded audio for Spark-TTS enrollment."""

from __future__ import annotations

import contextlib
import io
import logging
import os
import shutil
import subprocess  # noqa: S404 — ffmpeg, argv-only, never a shell string
import tempfile
import wave
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # numpy is an optional runtime dependency
    import numpy as np

logger = logging.getLogger(__name__)

TARGET_RATE = 16000

#: Maximum source audio used for enrollment.
MAX_SOURCE_SECONDS = 120

#: Sinc kernel half-width.
_SINC_ZEROS = 16


class UnsupportedAudio(ValueError):
    """The upload could not be decoded by any reader this install has."""


def ffmpeg_path() -> str | None:
    """``ffmpeg`` on PATH, or ``None``. Probed, never assumed."""
    return shutil.which("ffmpeg")


def _have_soundfile() -> bool:
    try:
        import soundfile  # noqa: F401, PLC0415 — deferred probe
    except Exception:
        return False
    return True


def _from_wave(data: bytes) -> tuple[np.ndarray, int] | None:
    """Decode PCM WAV with the stdlib. ``None`` when this is not that."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    try:
        with wave.open(io.BytesIO(data), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            frames = min(handle.getnframes(), rate * MAX_SOURCE_SECONDS)
            raw = handle.readframes(frames)
    except (wave.Error, EOFError):
        return None
    if not raw or channels < 1 or rate <= 0:
        return None
    if width == 3:
        # Widen 24-bit little-endian samples manually; NumPy has no 24-bit dtype.
        packed = np.frombuffer(raw[: len(raw) // 3 * 3], dtype=np.uint8).reshape(-1, 3)
        widened = np.zeros((packed.shape[0], 4), dtype=np.uint8)
        widened[:, 1:] = packed
        samples = widened.view("<i4").reshape(-1).astype(np.float32)
        width = 4
    else:
        dtype = {1: np.uint8, 2: "<i2", 4: "<i4"}.get(width)
        if dtype is None:
            return None
        samples = np.frombuffer(raw, dtype=dtype).astype(np.float32)
    if width == 1:  # 8-bit WAV is unsigned, centred on 128
        samples = (samples - 128.0) / 128.0
    else:
        samples /= float(1 << (8 * width - 1))
    return _to_mono(samples, channels), rate


def _from_soundfile(data: bytes) -> tuple[np.ndarray, int] | None:
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    try:
        import soundfile  # noqa: PLC0415 — deferred; optional dependency
    except Exception:
        return None
    try:
        with soundfile.SoundFile(io.BytesIO(data)) as handle:
            rate = int(handle.samplerate)
            block = handle.read(frames=rate * MAX_SOURCE_SECONDS, dtype="float32", always_2d=True)
    except Exception as exc:  # libsndfile says "format not recognised" for anything it lacks
        logger.debug("soundfile could not read the upload: %s", exc)
        return None
    if block.size == 0 or rate <= 0:
        return None
    return np.ascontiguousarray(block.mean(axis=1), dtype=np.float32), rate


def _from_ffmpeg(data: bytes, suffix: str) -> tuple[np.ndarray, int] | None:
    """Use ffmpeg to decode formats unsupported by the in-process readers."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    binary = ffmpeg_path()
    if binary is None:
        return None
    fd, path = tempfile.mkstemp(suffix=suffix or ".bin")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        # Pass a fixed argument list and a temporary input path.
        argv = [
            binary, "-nostdin", "-v", "error",
            "-t", str(MAX_SOURCE_SECONDS),
            "-i", path,
            "-map", "a:0",  # the audio of a video file, and only the first track
            "-f", "f32le", "-acodec", "pcm_f32le", "-ac", "1", "-ar", str(TARGET_RATE),
            "pipe:1",
        ]  # fmt: skip
        try:
            done = subprocess.run(argv, capture_output=True, timeout=120, check=False)  # noqa: S603 — argv-only, binary from which()
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("ffmpeg failed to decode the upload: %s", exc)
            return None
        if done.returncode != 0 or not done.stdout:
            logger.warning("ffmpeg rejected the upload: %s", (done.stderr or b"").decode("utf-8", "replace")[-400:])
            return None
        return np.frombuffer(done.stdout, dtype=np.float32).copy(), TARGET_RATE
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)


def _to_mono(interleaved: np.ndarray, channels: int) -> np.ndarray:
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    if channels <= 1:
        return np.ascontiguousarray(interleaved, dtype=np.float32)
    usable = (len(interleaved) // channels) * channels
    return np.ascontiguousarray(interleaved[:usable].reshape(-1, channels).mean(axis=1), dtype=np.float32)


def resample(wav: np.ndarray, src_rate: int, dst_rate: int = TARGET_RATE) -> np.ndarray:
    """Band-limited resample of a mono signal using a windowed-sinc kernel."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    if src_rate == dst_rate or wav.size == 0:
        return np.ascontiguousarray(wav, dtype=np.float32)
    ratio = dst_rate / float(src_rate)
    # Lower the cutoff for downsampling to avoid aliasing.
    cutoff = min(1.0, ratio) * 0.95
    half = int(np.ceil(_SINC_ZEROS / cutoff))
    taps = np.arange(-half + 1, half + 1, dtype=np.float64)
    out_len = max(1, int(np.floor(wav.size * ratio)))
    source = np.asarray(wav, dtype=np.float64)
    out = np.empty(out_len, dtype=np.float64)
    # Process blocks to bound the temporary weight matrix.
    block = 8192
    for start in range(0, out_len, block):
        stop = min(start + block, out_len)
        centre = np.arange(start, stop, dtype=np.float64) / ratio
        base = np.floor(centre).astype(np.int64)
        offsets = base[:, None] + taps[None, :]
        delta = offsets - centre[:, None]
        weights = cutoff * np.sinc(cutoff * delta) * np.kaiser(2 * half, 5.0)[None, :]
        # Clamp edge indices and zero their weights, equivalent to zero-padding.
        inside = (offsets >= 0) & (offsets < source.size)
        gathered = source[np.clip(offsets, 0, source.size - 1).astype(np.int64)]
        out[start:stop] = np.einsum("ij,ij->i", gathered * inside, weights)
    return out.astype(np.float32)


def volume_normalize(wav: np.ndarray, coeff: float = 0.2) -> np.ndarray:
    """Apply Spark-TTS's volume normalization."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    audio = np.asarray(wav, dtype=np.float32).reshape(-1).copy()
    if audio.size == 0:
        return audio
    temp = np.sort(np.abs(audio))
    if temp[-1] < 0.1:  # a very quiet recording is lifted before anything else
        audio = audio / max(float(temp[-1]), 1e-3) * 0.1
    temp = temp[temp > 0.01]
    if temp.shape[0] <= 10:  # too little signal to measure; leave it alone
        return audio
    length = temp.shape[0]
    volume = float(np.mean(temp[int(0.9 * length) : int(0.99 * length)]))
    audio = audio * np.clip(coeff / volume, 0.1, 10)
    peak = float(np.max(np.abs(audio)))
    if peak > 1:
        audio = audio / peak
    return audio.astype(np.float32)


def decode(data: bytes, *, filename: str = "") -> np.ndarray:
    """Decode uploaded audio as 16 kHz mono float32."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    if not data:
        raise UnsupportedAudio("The uploaded file is empty.")
    suffix = os.path.splitext(filename)[1].lower()
    for reader in (_from_wave, _from_soundfile):
        got = reader(data)
        if got is not None:
            signal, rate = got
            break
    else:
        got = _from_ffmpeg(data, suffix)
        if got is None:
            raise UnsupportedAudio(_unreadable_message(suffix))
        signal, rate = got
    if signal.size == 0:
        raise UnsupportedAudio("The uploaded file contains no audio.")
    signal = signal[: rate * MAX_SOURCE_SECONDS]
    resampled = resample(signal, rate, TARGET_RATE)
    # Keep silent or clipped uploads finite before mel extraction.
    return np.nan_to_num(resampled, copy=False, nan=0.0, posinf=1.0, neginf=-1.0)


def _unreadable_message(suffix: str) -> str:
    kind = suffix.lstrip(".").upper() or "That file"
    if _have_soundfile():
        return f"{kind} audio needs ffmpeg, which is not on PATH. Install ffmpeg, or upload a WAV or FLAC file."
    return f"{kind} audio could not be read. Upload a PCM WAV file, or install ffmpeg to accept any format."


def reference_signal(wav: np.ndarray, min_seconds: int = 6, hop: int = 320) -> np.ndarray:
    """Prepare a reference signal, repeating short clips and trimming frame tails."""
    import numpy as np  # noqa: PLC0415 — deferred; see module docstring

    minimum = int(TARGET_RATE * min_seconds) // hop * hop
    audio = np.asarray(wav, dtype=np.float32).reshape(-1)
    if audio.size == 0:
        return np.zeros(minimum, dtype=np.float32)
    if audio.size < minimum:
        return np.ascontiguousarray(np.tile(audio, minimum // audio.size + 1)[:minimum], dtype=np.float32)
    return np.ascontiguousarray(audio[: audio.size // hop * hop], dtype=np.float32)


__all__ = [
    "MAX_SOURCE_SECONDS",
    "TARGET_RATE",
    "UnsupportedAudio",
    "decode",
    "ffmpeg_path",
    "reference_signal",
    "resample",
    "volume_normalize",
]
