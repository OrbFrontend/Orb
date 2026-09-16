"""Built-in Spark-TTS enrollment and synthesis."""

from __future__ import annotations

from .audio_in import UnsupportedAudio
from .enroll import EnrollmentUnavailable

__all__ = [
    "EnrollmentUnavailable",
    "UnsupportedAudio",
]
