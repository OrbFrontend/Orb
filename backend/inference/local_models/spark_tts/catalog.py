"""Locate Spark-TTS artifacts and report their readiness."""

from __future__ import annotations

from .. import assets, dependencies, llama_server, onnx_runtime
from ..catalog import MODELS

FEATURE_LLM = "spark_tts_llm"
FEATURE_CODEC = "spark_tts_codec"


def decoder_path() -> str:
    """``bicodec.onnx``'s absolute path (may not exist)."""
    return assets.resolve_path(FEATURE_CODEC)


def speaker_encoder_path() -> str:
    """The speaker encoder's absolute path (may not exist)."""
    return assets.file_path(next(iter(MODELS[FEATURE_CODEC].extra_files)))


def codec_ready() -> tuple[bool, str]:
    """Return whether the codec files and runtime are available."""
    ok, reason = dependencies.deps_ok(FEATURE_CODEC)
    if not ok:
        return False, reason
    if not onnx_runtime.runtime_ok():
        return False, "onnxruntime is not installed."
    missing = assets.missing_files(FEATURE_CODEC)
    if missing:
        return False, f"Spark-TTS codec files are not downloaded: {', '.join(missing)}"
    return True, ""


def llm_ready() -> tuple[bool, str]:
    """Is there a GGUF on disk AND a llama-server binary to run it?"""
    ok, reason = dependencies.deps_ok(FEATURE_LLM)
    if not ok:
        return False, reason
    if assets.missing_files(FEATURE_LLM):
        return False, "The Spark-TTS model is not downloaded."
    if not llama_server.runtime_ok():
        return False, "The llama-server runtime is not installed."
    return True, ""


__all__ = [
    "FEATURE_CODEC",
    "FEATURE_LLM",
    "codec_ready",
    "decoder_path",
    "llm_ready",
    "speaker_encoder_path",
]
