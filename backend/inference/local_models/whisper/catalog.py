"""Locate the speech recognizer's files and report their readiness."""

from __future__ import annotations

import os

from .. import assets, dependencies, onnx_runtime
from ..catalog import MODELS
from .recognize import WhisperFiles

FEATURE = "speech_recognizer"


def files() -> WhisperFiles:
    """Return the downloaded checkpoint's paths."""
    by_name = {os.path.basename(f.path): assets.file_path(f) for f in MODELS[FEATURE].extra_files}
    decoder = next(path for name, path in by_name.items() if name.startswith("decoder_model_merged"))
    return WhisperFiles(
        encoder=assets.resolve_path(FEATURE),
        decoder=decoder,
        vocab=by_name["vocab.json"],
        config=by_name["config.json"],
        generation_config=by_name["generation_config.json"],
    )


def ready() -> tuple[bool, str]:
    """Return whether a clip can be transcribed right now."""
    ok, reason = dependencies.deps_ok(FEATURE)
    if not ok:
        return False, reason
    if not onnx_runtime.runtime_ok():
        return False, "onnxruntime is not installed."
    if assets.missing_files(FEATURE):
        return False, "The speech recognizer is not downloaded."
    return True, ""


__all__ = ["FEATURE", "files", "ready"]
