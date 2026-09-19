"""Offline speech recognition with Whisper's ONNX export."""

from __future__ import annotations

from .catalog import FEATURE, files, ready
from .recognize import MAX_SECONDS, Transcript, WhisperFiles, release, transcribe

__all__ = ["FEATURE", "MAX_SECONDS", "Transcript", "WhisperFiles", "files", "ready", "release", "transcribe"]
