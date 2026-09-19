"""Generate Spark-TTS tokens and decode them to audio."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

from ..llama_server import ManagedLlamaServerHost
from . import codec, config
from .reference import Reference
from .tokens import (
    STOP_TOKENS,
    clone_prompt,
    join_content,
    semantic_indices,
    token_budget,
    validate_reference_tokens,
    validate_speaker_tokens,
)

logger = logging.getLogger(__name__)

#: Shared Spark-TTS model host.
HOST = ManagedLlamaServerHost(name="spark_tts", idle_timeout=config.IDLE_TIMEOUT)

#: Spark-TTS sampling defaults.
TEMPERATURE = 0.8
TOP_P = 0.95
TOP_K = 50


class SynthesisFailed(ValueError):
    """Spark-TTS could not produce audio for this line."""


def state() -> dict[str, str]:
    """``{"state": idle|loading|ready|failed, "error": …}`` for status surfaces."""
    return {"state": HOST.state, "error": HOST.error}


async def synthesize(
    text: str,
    speaker_tokens: Sequence[int],
    *,
    reference: Reference | None = None,
    gpu: bool = True,
) -> tuple[bytes, int]:
    """Speak text in an enrolled voice and return ``(pcm16, sample_rate)``.

    With a *reference*, the prompt carries the reference excerpt's speech and
    transcript, and the line continues that delivery rather than only its
    timbre.
    """
    spoken = " ".join(text.split())
    if not spoken:
        return b"", codec.SAMPLE_RATE
    speaker = validate_speaker_tokens(list(speaker_tokens))
    content, excerpt = spoken, []
    if reference is not None:
        if not reference.text.strip():
            raise SynthesisFailed("The reference excerpt has no transcript.")
        content = join_content(reference.text, spoken)
        excerpt = validate_reference_tokens(list(reference.tokens))
    profile = config.launch_profile(gpu=gpu)
    async with HOST.use(profile) as server:
        # User text must not be interpreted as control tokens.
        body = await server.tokenize(content, parse_special=False)
        prompt = clone_prompt(body, speaker, excerpt)
        budget = min(token_budget(spoken), config.CTX_SIZE - len(prompt))
        if budget <= 0:
            raise SynthesisFailed("This line is too long for Spark-TTS to speak in one piece.")
        generated: list[int] = []
        if excerpt:
            # A continuation must not end before it starts. The first token
            # after an excerpt carries a small raw chance of ending (up to
            # about a quarter, measured on a slow speaker), and top-k keeps
            # that one token while discarding most of the probability spread
            # over 8192 audio tokens — leaving whole lines, the preview among
            # them, ending at their first token every time. So the first
            # token is sampled with ending banned, and the rest continues
            # from it unconstrained, off the prompt already in the cache.
            generated, _ = await server.generate_tokens(
                prompt,
                n_predict=1,
                temperature=TEMPERATURE,
                top_p=TOP_P,
                top_k=TOP_K,
                banned=STOP_TOKENS,
            )
        stopped = False
        if budget > len(generated):
            rest, stopped = await server.generate_tokens(
                prompt + generated,
                n_predict=budget - len(generated),
                temperature=TEMPERATURE,
                top_p=TOP_P,
                top_k=TOP_K,
            )
            generated += rest
    semantic = semantic_indices(generated)
    if not semantic:
        # Without semantic tokens there is nothing for the decoder to render.
        raise SynthesisFailed("Spark-TTS produced no audio tokens for this line.")
    if not stopped:
        logger.info("Spark-TTS hit its %d-token budget for a %d-character line", budget, len(spoken))
    # Keep the blocking decoder off the event loop.
    pcm = await asyncio.to_thread(codec.decode, semantic, speaker)
    return pcm, codec.SAMPLE_RATE


__all__ = ["HOST", "SynthesisFailed", "state", "synthesize"]
