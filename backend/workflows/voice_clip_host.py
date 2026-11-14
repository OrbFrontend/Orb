"""Bridge the TTS workflow to voice-clip preparation (vocal separation and level) for reference cloning."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from ..core.settings import Settings
from ..inference.local_models import voice_clip
from ..inference.local_models.spark_tts.audio_in import UnsupportedAudio
from .errors import WorkflowInputError

FEATURE = voice_clip.FEATURE


def separator_ready(settings: Settings) -> tuple[bool, str]:
    """Whether uploads can have their vocals separated, and why not."""
    raw = settings.get("local_ml_enabled")
    if isinstance(raw, Mapping) and raw.get(FEATURE, True) is False:
        return False, "The vocal separator is switched off."
    return voice_clip.ready()


async def prepare_clip(data: bytes, settings: Settings, *, filename: str = "") -> tuple[bytes, str]:
    """Prepare an uploaded clip off the event loop: the WAV, and what the panel says about sound under the voice.

    An unreadable or silent file raises ``WorkflowInputError``.
    """
    separate, reason = separator_ready(settings)

    def run() -> voice_clip.PreparedClip:
        try:
            return voice_clip.prepare(data, filename=filename, separate=separate)
        finally:
            if separate:
                voice_clip.release()

    try:
        clip = await asyncio.to_thread(run)
    except UnsupportedAudio as exc:
        raise WorkflowInputError(str(exc)) from exc
    if clip.separated and clip.background:
        return clip.wav, "Orb took out what music it could, but sound remains under the voice, and the clone may copy it."
    if clip.separated:
        return clip.wav, "Orb removed the background sound under the voice."
    if clip.background:
        return clip.wav, f"Something plays under the voice, and the clone will copy it. {reason}"
    return clip.wav, ""
