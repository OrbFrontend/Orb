"""Resolve the Spark-TTS llama-server launch profile."""

from __future__ import annotations

import os

from .. import assets
from ..catalog import MODELS
from ..llama_server import LaunchProfile
from . import catalog
from .tokens import TOKEN_CEILING

#: What the child calls itself in its own logs and on /v1/models.
ALIAS = "spark-tts"

#: One lane. TTS synthesises one chunk at a time by construction — the workflow
#: stitches clips in order and each needs the previous one's length to place the
#: next — so extra slots would divide the context for lanes nothing fills.
PARALLEL = 1

#: Prompt plus generation, with room for both. The cloning prompt is ~40 control
#: and speaker tokens plus the line itself, and generation is capped at
#: TOKEN_CEILING, so this is the ceiling plus a comfortable prompt.
CTX_SIZE = TOKEN_CEILING + 1096

HTTP_THREADS = 4

#: At most 8 tokens per forward pass. NOT A PERFORMANCE KNOB — A CORRECTNESS ONE.
#: ggml-vulkan multiplies batches of more than 8 rows with a different kernel,
#: and for this Qwen2-0.5B that kernel returns wrong feed-forward results
#: (RTX 3090, NVIDIA 580, llama.cpp b10549 and b11000, Q8_0 and F16 alike; the
#: Qwen3 prose rewriters are unaffected). The prompt is the only multi-token
#: batch, so the damage lands exactly there: the model misses
#: <|start_semantic_token|>, emits a second or two of babble and stops. Nothing
#: raises. At 8 the GPU matches the CPU build to within 0.07 logprob and keeps
#: its ~380 tok/s against the CPU's 55. The prompt is ~60 tokens, so the cap
#: costs nothing measurable. It applies on the CPU path too, because a Vulkan
#: build from PATH still sends a big batch to the GPU at --n-gpu-layers 0.
UBATCH_SIZE = 8

#: Seconds at zero in-flight before the child is stopped and its VRAM released.
#: SHORTER THAN THE PROSE REWRITER'S ON PURPOSE. Speech is bursty, this model is
#: small, and the rewriter it shares a card with is 2-4 GB — on a 6 GB card,
#: holding 520 MB against that costs more than paying a reload. Reloading is
#: seconds; thrashing the rewriter is the whole turn.
IDLE_TIMEOUT = float(os.environ.get("ORB_SPARK_TTS_IDLE", "120"))


def launch_profile(*, gpu: bool = True) -> LaunchProfile:
    """The one constructor of a Spark-TTS ``LaunchProfile`` — and its trust barrier.

    The path is resolved through the shared asset store from a catalog entry
    this module names as a literal, so nothing that arrived over HTTP can reach
    a command line. The generic client has no feature catalog to check against,
    which is why this check lives here rather than travelling with the argv.
    """
    spec = MODELS[catalog.FEATURE_LLM]
    path = assets.resolve_path(catalog.FEATURE_LLM)
    if not os.path.exists(path):
        raise RuntimeError(f"Spark-TTS is not downloaded — {spec.local_name} is missing.")
    return LaunchProfile(
        model_id=catalog.FEATURE_LLM,
        model_path=path,
        alias=ALIAS,
        # GPU vs CPU is this one number. Vulkan is a property of which binary
        # was fetched, not a runtime switch.
        gpu_layers=999 if gpu else 0,
        ctx_size=CTX_SIZE,
        parallel=PARALLEL,
        http_threads=HTTP_THREADS,
        ubatch_size=UBATCH_SIZE,
        label="Spark-TTS 0.5B",
        size_mb=spec.size_mb,
    )


__all__ = ["ALIAS", "CTX_SIZE", "IDLE_TIMEOUT", "PARALLEL", "UBATCH_SIZE", "launch_profile"]
