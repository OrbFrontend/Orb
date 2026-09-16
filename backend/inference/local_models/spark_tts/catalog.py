"""Where Spark-TTS's three artifacts live, and whether they are usable.

The feature is deliberately split across two catalog entries because the halves
run on different runtimes: the LLM is a child llama-server (and therefore the
one part Orb's Vulkan build accelerates), while the codec is a pair of ONNX
graphs on the CPU. A machine can legitimately have one and not the other, and
the readiness answers below keep that distinguishable instead of collapsing it
into "Spark-TTS is not installed".
"""

from __future__ import annotations

from .. import assets, dependencies, llama_server, onnx_runtime
from ..catalog import MODELS

FEATURE_LLM = "spark_tts_llm"
FEATURE_CODEC = "spark_tts_codec"

#: The basename of the speaker encoder inside the codec spec. Named here so the
#: export script and the enrollment path cannot drift apart.
SPEAKER_ENCODER_NAME = next(iter(MODELS[FEATURE_CODEC].extra_files)).local_name


def llm_path() -> str:
    """The GGUF's absolute path (may not exist)."""
    return assets.resolve_path(FEATURE_LLM)


def decoder_path() -> str:
    """``bicodec.onnx``'s absolute path (may not exist)."""
    return assets.resolve_path(FEATURE_CODEC)


def speaker_encoder_path() -> str:
    """The speaker encoder's absolute path (may not exist)."""
    return assets.file_path(next(iter(MODELS[FEATURE_CODEC].extra_files)))


def codec_ready() -> tuple[bool, str]:
    """Can a voice be enrolled and a token stream be rendered?

    Enrollment needs only this half, which is why it is asked separately: a
    user can upload a clip and see their 32 tokens stored before the 520 MB LLM
    has finished downloading.
    """
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


def runnable() -> tuple[bool, str]:
    """Both halves. What synthesis requires; enrollment requires only the codec."""
    for check in (llm_ready, codec_ready):
        ok, reason = check()
        if not ok:
            return False, reason
    return True, ""


__all__ = [
    "FEATURE_CODEC",
    "FEATURE_LLM",
    "SPEAKER_ENCODER_NAME",
    "codec_ready",
    "decoder_path",
    "llm_path",
    "llm_ready",
    "runnable",
    "speaker_encoder_path",
]
