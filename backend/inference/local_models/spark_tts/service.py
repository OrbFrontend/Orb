"""Run Spark-TTS: prompt the child, read token ids, render a waveform.

The two halves meet here. ``ManagedLlamaServerHost`` already supports a second
resident child — ``manager`` holds a LIST of hosts, and the app lifespan stops
every one of them — so this is a supported pattern rather than new machinery.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from ..llama_server import ManagedLlamaServerHost
from . import codec, config
from .tokens import (
    clone_prompt,
    semantic_indices,
    token_budget,
    validate_speaker_tokens,
)

logger = logging.getLogger(__name__)

#: One host per process, registered with the shared runtime manager so the app
#: lifespan can stop a child it otherwise knows nothing about.
HOST = ManagedLlamaServerHost(name="spark_tts", idle_timeout=config.IDLE_TIMEOUT)

#: Sampling. Upstream's defaults, which is the right default for a model whose
#: published samples were produced with them.
TEMPERATURE = 0.8
TOP_P = 0.95
TOP_K = 50


class SynthesisFailed(ValueError):
    """Spark-TTS could not produce audio for this line."""


def state() -> dict[str, str]:
    """``{"state": idle|loading|ready|failed, "error": …}`` for status surfaces."""
    return {"state": HOST.state, "error": HOST.error}


async def synthesize(text: str, speaker_tokens: Sequence[int], *, gpu: bool = True) -> tuple[bytes, int]:
    """Speak *text* in the enrolled voice. Returns ``(pcm16, sample_rate)``.

    Note what is NOT sent: no reference audio, no transcript, no semantic
    tokens, and no per-request re-encoding. The 32 ints came out of the
    database, and everything the model needs about the voice is in them.
    """
    spoken = " ".join(text.split())
    if not spoken:
        return b"", codec.SAMPLE_RATE
    speaker = validate_speaker_tokens(list(speaker_tokens))
    profile = config.launch_profile(gpu=gpu)
    async with HOST.use(profile) as server:
        # parse_special OFF: the line is a character's dialogue, not a place to
        # honour control-token spellings that happen to appear in it.
        body = await server.tokenize(spoken, parse_special=False)
        prompt = clone_prompt(body, speaker)
        budget = token_budget(spoken)
        generated, stopped = await server.generate_tokens(
            prompt,
            n_predict=budget,
            temperature=TEMPERATURE,
            top_p=TOP_P,
            top_k=TOP_K,
        )
    semantic = semantic_indices(generated)
    if not semantic:
        # Upstream retries this, because on ITS path the failure is the model
        # emitting no speaker tokens and it has no other source for them. Here
        # the speaker tokens are supplied, so an empty generation is a real
        # failure rather than a dice roll worth re-rolling.
        raise SynthesisFailed("Spark-TTS produced no audio tokens for this line.")
    if not stopped:
        logger.info("Spark-TTS hit its %d-token budget for a %d-character line", budget, len(spoken))
    # The 385 MB decoder holds the GIL for its whole run; off the loop it goes,
    # or every other request in the app stalls for the length of the clip.
    pcm = await asyncio.to_thread(codec.decode, semantic, speaker)
    return pcm, codec.SAMPLE_RATE


__all__ = ["HOST", "SynthesisFailed", "state", "synthesize"]
