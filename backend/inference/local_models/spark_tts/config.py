"""Resolve the Spark-TTS llama-server launch profile."""

from __future__ import annotations

import os

from .. import assets
from ..catalog import MODELS
from ..llama_server import LaunchProfile
from . import catalog
from .tokens import MAX_REFERENCE_TEXT, MAX_REFERENCE_TOKENS, TOKEN_CEILING

#: Child model alias.
ALIAS = "spark-tts"

#: TTS synthesizes chunks serially.
PARALLEL = 1

#: What an advanced voice adds to the prompt: the start-of-speech token, the
#: reference excerpt, and its transcript at up to two tokens a character.
REFERENCE_ALLOWANCE = 1 + MAX_REFERENCE_TOKENS + 2 * MAX_REFERENCE_TEXT

#: Context for the prompt and maximum generation.
CTX_SIZE = TOKEN_CEILING + 1096 + REFERENCE_ALLOWANCE

HTTP_THREADS = 4

#: Larger batches produce incorrect output with the Vulkan build.
UBATCH_SIZE = 8

#: Seconds without work before releasing the model.
IDLE_TIMEOUT = float(os.environ.get("ORB_SPARK_TTS_IDLE", "120"))


def launch_profile(*, gpu: bool = True) -> LaunchProfile:
    """Build a launch profile for the downloaded Spark-TTS model."""
    spec = MODELS[catalog.FEATURE_LLM]
    path = assets.resolve_path(catalog.FEATURE_LLM)
    if not os.path.exists(path):
        raise RuntimeError(f"Spark-TTS is not downloaded — {spec.local_name} is missing.")
    return LaunchProfile(
        model_id=catalog.FEATURE_LLM,
        model_path=path,
        alias=ALIAS,
        # The runtime selects GPU use through the layer count.
        gpu_layers=999 if gpu else 0,
        ctx_size=CTX_SIZE,
        parallel=PARALLEL,
        http_threads=HTTP_THREADS,
        ubatch_size=UBATCH_SIZE,
        label="Spark-TTS 0.5B",
        size_mb=spec.size_mb,
    )


__all__ = ["ALIAS", "CTX_SIZE", "IDLE_TIMEOUT", "PARALLEL", "REFERENCE_ALLOWANCE", "UBATCH_SIZE", "launch_profile"]
