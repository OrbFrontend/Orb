"""
backend/tts/router.py — TTS adapter registry and routing.

Maps backend names (e.g. 'edge', 'openai', 'fish') to adapter classes.
Adapters are registered only if their dependencies are available.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import TTSAdapter

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[TTSAdapter]] = {}

# ── Register adapters (graceful — skip if dependency missing) ──────────────

try:
    from .edge_adapter import EdgeTTSAdapter

    _REGISTRY["edge"] = EdgeTTSAdapter
except ImportError:
    logger.info("edge-tts not installed — Edge TTS backend disabled")

try:
    from .elevenlabs_adapter import ElevenLabsAdapter

    _REGISTRY["elevenlabs"] = ElevenLabsAdapter
except ImportError:
    logger.info("httpx not installed — ElevenLabs backend disabled")

try:
    from .fish_adapter import FishSpeechAdapter

    _REGISTRY["fish"] = FishSpeechAdapter
except ImportError:
    logger.info("httpx not installed — Fish Speech backend disabled")

try:
    from .openai_speech_adapter import OpenAISpeechAdapter

    _REGISTRY["openai"] = OpenAISpeechAdapter
except ImportError:
    logger.info("httpx not installed — OpenAI TTS backend disabled")


def get_adapter(backend: str) -> TTSAdapter:
    """Instantiate and return a TTS adapter for the given backend name."""
    cls = _REGISTRY.get(backend)
    if not cls:
        available = ", ".join(_REGISTRY.keys()) or "(none installed)"
        raise ValueError(f"Unknown TTS backend '{backend}'. Available: {available}")
    return cls()


def list_backends() -> list[dict]:
    """Return info about all registered backends."""
    result = []
    for name, cls in _REGISTRY.items():
        instance = cls()
        result.append(
            {
                "id": name,
                "name": instance.backend_name,
                "supports_streaming": instance.supports_streaming,
                "supports_emotion_tags": instance.supports_emotion_tags,
            }
        )
    return result
