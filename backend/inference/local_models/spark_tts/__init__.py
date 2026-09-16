"""Spark-TTS as a built-in voice cloner: enrollment, synthesis, and its runtime.

A "cloned voice" here is 32 integers. BiCodec's speaker encoder reads six
seconds of a reference clip and emits them; the LLM is prompted with them and
continues a stream of semantic tokens for the new text; the BiCodec decoder
renders that stream using those same 32 as timbre. No training, no reference
audio at synthesis time, and no torch anywhere on the path.

Deliberately NOT included: the transcript-conditioned path, where the prompt
also carries the reference's semantic tokens so the model copies its speaking
RATE as well as its timbre. That needs wav2vec2 and the BiCodec encoder, 1.4 GB
for a measured difference of 0.007 in timbre similarity — inside seed noise —
and a ~18% change in delivery pace. Timbre is what a voice is; pace is what a
performance is. See docs/plans/spark-tts-builtin-cloner.md.
"""

from __future__ import annotations

from . import audio_in, catalog, codec, config, enroll, mel, service, tokens
from .audio_in import UnsupportedAudio
from .catalog import codec_ready, llm_ready, runnable
from .codec import SAMPLE_RATE, EmptyGeneration
from .enroll import EnrollmentUnavailable
from .service import HOST, SynthesisFailed
from .tokens import (
    SPEAKER_TOKEN_COUNT,
    InvalidSpeakerTokens,
    looks_like_speaker_tokens,
    validate_speaker_tokens,
)

__all__ = [
    "HOST",
    "SAMPLE_RATE",
    "SPEAKER_TOKEN_COUNT",
    "EmptyGeneration",
    "EnrollmentUnavailable",
    "InvalidSpeakerTokens",
    "SynthesisFailed",
    "UnsupportedAudio",
    "audio_in",
    "catalog",
    "codec",
    "codec_ready",
    "config",
    "enroll",
    "llm_ready",
    "looks_like_speaker_tokens",
    "mel",
    "runnable",
    "service",
    "tokens",
    "validate_speaker_tokens",
]
