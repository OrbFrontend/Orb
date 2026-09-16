"""Route TTS requests to the configured adapter."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import TTSAdapter

logger = logging.getLogger(__name__)

_REGISTRY: dict[str, type[TTSAdapter]] = {}


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

try:
    from .kokoro_adapter import KokoroTTSAdapter

    _REGISTRY["kokoro"] = KokoroTTSAdapter
except ImportError:
    logger.info("httpx not installed — Kokoro TTS backend disabled")

# Two Spark-TTS backends, and which name is which matters for existing saves.
# `spark` is the BUILT-IN cloner: no sidecar, no api_url, a voice enrolled from
# an uploaded clip. `spark_remote` is the HTTP client for the standalone
# OrbTTS sidecar that came first; profiles written before the built-in existed
# are migrated onto it (migration 0062) so they keep working against a server
# the user already runs, rather than being silently repointed at a model that
# may not be downloaded.
try:
    from .builtin_spark_adapter import BuiltinSparkAdapter

    _REGISTRY["spark"] = BuiltinSparkAdapter
except ImportError:  # the toolkit surface it needs is part of the app, so this is a packaging fault
    logger.info("Spark-TTS built-in backend unavailable")

try:
    from .spark_adapter import SparkTTSAdapter

    _REGISTRY["spark_remote"] = SparkTTSAdapter
except ImportError:
    logger.info("httpx not installed — Spark-TTS sidecar backend disabled")


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
