"""Helpers for adapters that stitch PCM clips into WAV."""

from __future__ import annotations

import io
import wave
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import SpeakableChunk


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


async def stitch_pcm(
    chunks: Sequence[SpeakableChunk],
    synthesize: Callable[[SpeakableChunk], Awaitable[tuple[bytes, int]]],
    fallback_rate: int = 16000,
) -> tuple[bytes, int]:
    """Synthesize chunks and insert their requested pauses."""
    text_chunks = [chunk for chunk in chunks if chunk.text.strip()]
    if not text_chunks:
        return b"", fallback_rate

    parts: list[bytes] = []
    sample_rate = fallback_rate
    for index, chunk in enumerate(text_chunks):
        if index and chunk.pause_before_ms > 0:
            parts.append(silence_pcm(chunk.pause_before_ms, sample_rate))
        pcm, clip_rate = await synthesize(chunk)
        if index == 0:
            sample_rate = clip_rate
        parts.append(pcm)
        if chunk.pause_after_ms > 0:
            parts.append(silence_pcm(chunk.pause_after_ms, sample_rate))
    return b"".join(parts), sample_rate


__all__ = ["pcm_duration_ms", "pcm_to_wav", "silence_pcm", "stitch_pcm", "strip_header"]
