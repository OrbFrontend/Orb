"""
backend/tts/__init__.py — TTS backend abstraction layer.

Provides a common interface for multiple TTS backends (Edge, Fish, ElevenLabs, etc.)
with a router that selects the right adapter based on voice profile configuration.
"""

from .base import AudioChunk, SpeakableChunk, SynthesisResult, TTSAdapter
from .router import get_adapter, list_backends

__all__ = [
    "TTSAdapter",
    "SpeakableChunk",
    "AudioChunk",
    "SynthesisResult",
    "get_adapter",
    "list_backends",
]
