"""Bridge the TTS workflow to the built-in Spark-TTS model slice."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..database import get_settings, set_local_ml_config
from ..inference.local_models.spark_tts import (
    catalog,
    config,
    enroll,
    service,
    tokens,
)

logger = logging.getLogger(__name__)

FEATURE_LLM = catalog.FEATURE_LLM
FEATURE_CODEC = catalog.FEATURE_CODEC


def clean_tokens(raw: object) -> list[int]:
    """Return validated stored speaker tokens, or ``[]`` if invalid."""
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
        return False, "The Spark-TTS voice codec is switched off. Turn it on from the cloned voice in TTS settings."
    return catalog.codec_ready()


def synthesis_ready(settings: Mapping[str, Any]) -> tuple[bool, str]:
    """Can a line be spoken right now? Needs both halves and both toggles."""
    if not _enabled(settings, FEATURE_LLM):
        return False, "The Spark-TTS voice model is switched off. Turn it on from the cloned voice in TTS settings."
    ok, reason = enrollment_ready(settings)
    if not ok:
        return False, reason
    return catalog.llm_ready()


async def enroll_upload(data: bytes, *, filename: str = "") -> list[int]:
    """Enroll an uploaded audio file without blocking the event loop."""

    def run() -> list[int]:
        return enroll.enroll(data, filename=filename)

    return await asyncio.to_thread(run)


async def synthesize(text: str, speaker_tokens: Sequence[int], settings: Mapping[str, Any]) -> tuple[bytes, int]:
    """Speak *text* in an enrolled voice. Returns ``(pcm16, sample_rate)``."""
    ok, reason = synthesis_ready(settings)
    if not ok:
        raise service.SynthesisFailed(reason)
    return await service.synthesize(text, speaker_tokens, gpu=use_gpu(settings))


class _LlmManagement:
    """Local ML route hooks for the llama-server half."""

    async def status_extra(self, settings: Mapping[str, Any]) -> dict:
        return {"gpu": use_gpu(settings), **service.state()}

    async def apply_config(self, body: Mapping[str, Any]) -> dict:
        gpu = bool(body.get("gpu", True))
        await set_local_ml_config(FEATURE_LLM, {"gpu": gpu})
        # Apply the new profile on the next synthesis.
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


LLM_MANAGEMENT = _LlmManagement()


__all__ = [
    "FEATURE_CODEC",
    "FEATURE_LLM",
    "LLM_MANAGEMENT",
    "clean_tokens",
    "enroll_upload",
    "enrollment_ready",
    "synthesis_ready",
    "synthesize",
    "use_gpu",
]
