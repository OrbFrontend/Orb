"""Write an enrolled voice into a character's TTS profile, or forget it."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .engine.builtin_spark_adapter import VOICE_ID
from .synth import CLONE_MODES


def apply_voice(profile: dict, voice: Mapping[str, Any] | None, source_name: str, mode: object = None) -> dict:
    """Store *voice* (as ``spark_voice_enroll`` returns it) in *profile*, or clear it when ``None``."""
    profile["speaker_tokens"] = list(voice["speaker_tokens"]) if voice else []
    profile["speaker_ref_name"] = source_name
    profile["reference_tokens"] = list(voice["reference_tokens"]) if voice else []
    profile["reference_text"] = voice["reference_text"] if voice else ""
    if mode in CLONE_MODES:
        profile["clone_mode"] = mode
    if voice:
        # Uploading a voice selects the built-in backend and voice.
        profile["backend"] = "spark"
        profile["voice_id"] = VOICE_ID
        profile["enabled"] = True
    else:
        # Keep the backend selected for the next upload, but disable speech.
        profile["enabled"] = False
    return profile
