"""Host integration for the built-in Spark-TTS voice cloner.

The TTS workflow plug-in may import only ``workflows.toolkit``, so this is
where the plug-in's two needs — "enroll this upload" and "speak this line in
that voice" — are joined to the model slice under ``inference/local_models``.
It is also where the Local ML card's management hooks live for the feature's
two halves.

Enrollment and synthesis are gated SEPARATELY on purpose. The codec half is
23 MB + 368 MB of CPU graphs and is all that enrollment needs, so a user can
upload a clip and hear their stored tokens confirmed while the 520 MB LLM is
still downloading.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from collections.abc import Mapping, Sequence
from typing import Any

from ..database import get_settings, set_local_ml_config
from ..inference.local_models import assets, onnx_runtime
from ..inference.local_models.spark_tts import (
    catalog,
    codec,
    config,
    enroll,
    service,
    tokens,
)

logger = logging.getLogger(__name__)

FEATURE_LLM = catalog.FEATURE_LLM
FEATURE_CODEC = catalog.FEATURE_CODEC

#: Cap on a stored reference clip. The window is a fixed six seconds of 16 kHz
#: mono PCM — 192 KB — so this is headroom for a header and nothing else; it
#: exists so a bug upstream of the encoder cannot put an unbounded blob in a
#: row that is read whenever a card is opened.
MAX_REFERENCE_BYTES = 1024 * 1024


def clean_tokens(raw: object) -> list[int]:
    """A stored voice as 32 validated speaker tokens, or ``[]`` for anything else.

    The lenient half of the token contract: profile normalisation runs over
    whatever JSON is in the database and must not raise on a blob written by an
    older version, a hand-edited row, or a failed enrollment. The strict half —
    :func:`spark_tts.tokens.validate_speaker_tokens` — still guards the codec.
    """
    try:
        return tokens.validate_speaker_tokens(raw)
    except tokens.InvalidSpeakerTokens:
        return []


def _stored(settings: Mapping[str, Any], feature: str) -> dict:
    configs = settings.get("local_ml_config")
    stored = configs.get(feature) if isinstance(configs, Mapping) else None
    return dict(stored) if isinstance(stored, Mapping) else {}


def _enabled(settings: Mapping[str, Any], feature: str) -> bool:
    raw = settings.get("local_ml_enabled")
    return not isinstance(raw, Mapping) or raw.get(feature, True) is not False


def use_gpu(settings: Mapping[str, Any]) -> bool:
    """Whether the child is asked for a GPU build. Defaults on, like the rewriter."""
    return bool(_stored(settings, FEATURE_LLM).get("gpu", True))


def enrollment_ready(settings: Mapping[str, Any]) -> tuple[bool, str]:
    """Can a clip be turned into 32 speaker tokens right now?"""
    if not _enabled(settings, FEATURE_CODEC):
        return False, "The Spark-TTS voice codec is switched off in Local ML."
    return catalog.codec_ready()


def synthesis_ready(settings: Mapping[str, Any]) -> tuple[bool, str]:
    """Can a line be spoken right now? Needs both halves and both toggles."""
    if not _enabled(settings, FEATURE_LLM):
        return False, "Spark-TTS is switched off in Local ML."
    ok, reason = enrollment_ready(settings)
    if not ok:
        return False, reason
    return catalog.llm_ready()


def reference_wav(window) -> bytes:
    """The enrolled six-second window as a WAV file, for storage and preview."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(codec.SAMPLE_RATE)
        handle.writeframes(codec.to_pcm16(window))
    return buffer.getvalue()


async def enroll_upload(data: bytes, *, filename: str = "") -> tuple[list[int], bytes]:
    """``(speaker_tokens, reference_wav)`` for an uploaded audio file.

    Blocking work — decode, resample, mel, and a 23 MB ONNX session — goes to a
    thread: it is ~14 ms of model plus however long the file takes to decode,
    and an upload is not a reason to stall every other request in the app.
    """

    def run() -> tuple[list[int], bytes]:
        speaker, window = enroll.enroll(data, filename=filename)
        clip = reference_wav(window)
        if len(clip) > MAX_REFERENCE_BYTES:
            # Unreachable while the window is a fixed six seconds; asserted
            # anyway because the row it lands in is read whenever a card is
            # opened, and a change to ref_segment_duration must not silently
            # start putting megabytes there.
            raise RuntimeError(f"reference clip is {len(clip)} bytes, over the {MAX_REFERENCE_BYTES}-byte cap")
        return speaker, clip

    return await asyncio.to_thread(run)


async def synthesize(text: str, speaker_tokens: Sequence[int], settings: Mapping[str, Any]) -> tuple[bytes, int]:
    """Speak *text* in an enrolled voice. Returns ``(pcm16, sample_rate)``."""
    ok, reason = synthesis_ready(settings)
    if not ok:
        raise service.SynthesisFailed(reason)
    return await service.synthesize(text, speaker_tokens, gpu=use_gpu(settings))


class _LlmManagement:
    """Local ML card hooks for the llama-server half."""

    async def status_extra(self, settings: Mapping[str, Any]) -> dict:
        return {"gpu": use_gpu(settings), **service.state()}

    async def sync_selection(self, *, prefer: str | None = None) -> dict:
        """No variants to repair — the selection is a single file."""
        settings = await get_settings()
        return settings.get("local_ml_config", {})

    async def apply_config(self, body: Mapping[str, Any]) -> dict:
        gpu = bool(body.get("gpu", True))
        await set_local_ml_config(FEATURE_LLM, {"gpu": gpu})
        # Record the change without touching a child that may be mid-line; the
        # swap happens on the next synthesis, exactly as it does for a rewrite.
        try:
            service.HOST.mark_stale(config.launch_profile(gpu=gpu))
        except RuntimeError:  # not downloaded yet: nothing to point at, and that is fine
            service.HOST.mark_stale(None)
        settings = await get_settings()
        return settings.get("local_ml_config", {})

    async def on_enabled(self, enabled: bool) -> None:
        if not enabled:
            await service.HOST.release()

    async def release_host(self) -> None:
        """Release the mmap and child process before deleting the GGUF."""
        await service.HOST.release()


class _CodecManagement:
    """Local ML card hooks for the ONNX half."""

    async def status_extra(self, settings: Mapping[str, Any]) -> dict:
        missing = assets.missing_files(FEATURE_CODEC)
        return {"missing_files": missing, "providers": onnx_runtime.preferred_providers()}

    async def sync_selection(self, *, prefer: str | None = None) -> dict:
        settings = await get_settings()
        return settings.get("local_ml_config", {})

    async def apply_config(self, body: Mapping[str, Any]) -> dict:
        settings = await get_settings()
        return settings.get("local_ml_config", {})

    async def on_enabled(self, enabled: bool) -> None:
        if not enabled:
            onnx_runtime.release()

    async def release_host(self) -> None:
        """Drop the cached sessions before the files are deleted.

        Not optional on Windows, where an open handle makes the unlink fail
        outright; everywhere else it is 385 MB that would otherwise stay
        resident pointing at a file that no longer exists.
        """
        onnx_runtime.release()


LLM_MANAGEMENT = _LlmManagement()
CODEC_MANAGEMENT = _CodecManagement()


async def shutdown() -> None:
    """Stop the child and drop the ONNX sessions. For the app lifespan."""
    await service.shutdown()
    onnx_runtime.release()


__all__ = [
    "CODEC_MANAGEMENT",
    "FEATURE_CODEC",
    "FEATURE_LLM",
    "LLM_MANAGEMENT",
    "MAX_REFERENCE_BYTES",
    "clean_tokens",
    "enroll_upload",
    "enrollment_ready",
    "reference_wav",
    "shutdown",
    "synthesis_ready",
    "synthesize",
    "use_gpu",
]
