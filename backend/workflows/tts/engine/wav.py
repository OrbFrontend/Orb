"""WAV helpers shared by adapters that stitch per-chunk clips together.

Backends that synthesize one clip per speech chunk need real silence between
the clips to honour a chunk's pause hints. Working in raw PCM keeps that exact:
strip each clip's header, join, pad, and re-wrap once at the end.
"""

from __future__ import annotations

import io
import wave


def strip_header(wav_bytes: bytes) -> tuple[bytes, int]:
    """Return a WAV file's raw frames and its sample rate."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
        return handle.readframes(handle.getnframes()), handle.getframerate()


def silence_pcm(duration_ms: int, sample_rate: int) -> bytes:
    """Generate silent 16-bit mono PCM frames."""
    return b"\x00\x00" * int(sample_rate * duration_ms / 1000)


def pcm_to_wav(raw_pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw 16-bit mono PCM in a WAV container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(raw_pcm)
    return buffer.getvalue()


def pcm_duration_ms(raw_pcm: bytes, sample_rate: int) -> int:
    """Duration of 16-bit mono PCM, in milliseconds."""
    if sample_rate <= 0:
        return 0
    return round(len(raw_pcm) / 2 / sample_rate * 1000)


__all__ = ["pcm_duration_ms", "pcm_to_wav", "silence_pcm", "strip_header"]
