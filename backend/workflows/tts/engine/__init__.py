"""Text-to-speech adapter interface and router."""

from .base import SpeakableChunk, SynthesisResult, TTSAdapter
from .router import get_adapter, list_backends
