"""Bridge the TTS workflow to the built-in Spark-TTS model slice."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..database import get_settings, set_local_ml_config
from ..inference.local_models import whisper
from ..inference.local_models.spark_tts import (
    audio_in,
    catalog,
    codec,
    config,
    enroll,
    reference,
    service,
    tokens,
)

logger = logging.getLogger(__name__)

FEATURE_LLM = catalog.FEATURE_LLM
FEATURE_CODEC = catalog.FEATURE_CODEC
FEATURE_REFERENCE = catalog.FEATURE_REFERENCE
FEATURE_SPEECH_RECOGNIZER = whisper.FEATURE


def clean_tokens(raw: object) -> list[int]:
    """Return validated stored speaker tokens, or ``[]`` if invalid."""
    try:
        return tokens.validate_speaker_tokens(raw)
    except tokens.InvalidSpeakerTokens:
        return []


def clean_reference_tokens(raw: object) -> list[int]:
    """Return a stored reference excerpt's validated tokens, or ``[]`` if invalid."""
    try:
        return tokens.validate_reference_tokens(raw)
    except tokens.InvalidReferenceTokens:
        return []


def clean_reference_text(raw: object) -> str:
    """A stored transcript on one line, cut to the longest a reference can say."""
    return " ".join(str(raw or "").split())[: tokens.MAX_REFERENCE_TEXT]


def _stored(settings: Mapping[str, Any], feature: str) -> dict:
    configs = settings.get("local_ml_config")
    stored = configs.get(feature) if isinstance(configs, Mapping) else None
    return dict(stored) if isinstance(stored, Mapping) else {}


def _enabled(settings: Mapping[str, Any], feature: str) -> bool:
    raw = settings.get("local_ml_enabled")
    return not isinstance(raw, Mapping) or raw.get(feature, True) is not False


def use_gpu(settings: Mapping[str, Any]) -> bool:
    """Whether the child is asked for a GPU build. Defaults on, like the rewriter."""
    return bool(_stored(settings, FEATURE_LLM).get("gpu", True))


def enrollment_ready(settings: Mapping[str, Any]) -> tuple[bool, str]:
    """Can a clip be turned into 32 speaker tokens right now?"""
    if not _enabled(settings, FEATURE_CODEC):
        return False, "The Spark-TTS voice codec is switched off. Turn it on from the cloned voice in TTS settings."
    return catalog.codec_ready()


def reference_ready(settings: Mapping[str, Any]) -> tuple[bool, str]:
    """Can an enrollment also prepare an advanced reference right now?

    Needs the codec, the reference reader and the speech recognizer, and all
    three switched on. Speaking an advanced voice needs none of the last two.
    """
    ok, reason = enrollment_ready(settings)
    if not ok:
        return False, reason
    for feature, label in ((FEATURE_REFERENCE, "reference reader"), (FEATURE_SPEECH_RECOGNIZER, "speech recognizer")):
        if not _enabled(settings, feature):
            return False, f"The {label} is switched off. Turn it on from the cloned voice in TTS settings."
    ok, reason = catalog.reference_ready()
    if not ok:
        return False, reason
    return whisper.ready()


def synthesis_ready(settings: Mapping[str, Any]) -> tuple[bool, str]:
    """Can a line be spoken right now? Needs both halves and both toggles."""
    if not _enabled(settings, FEATURE_LLM):
        return False, "The Spark-TTS voice model is switched off. Turn it on from the cloned voice in TTS settings."
    ok, reason = enrollment_ready(settings)
    if not ok:
        return False, reason
    return catalog.llm_ready()


@dataclass(frozen=True)
class Enrollment:
    """What one uploaded clip became."""

    speaker_tokens: list[int]
    #: The advanced reference: an excerpt's semantic tokens and its transcript.
    reference_tokens: list[int] = field(default_factory=list)
    reference_text: str = ""
    #: Why the reference is missing or needs a transcript typed, when one was
    #: asked for. Enrollment itself still succeeded.
    reference_note: str = ""


def _prepare_reference(wav: Any) -> tuple[list[int], str, str]:
    """``(tokens, transcript, note)`` for the best excerpt of a decoded clip.

    Never raises for the clip's sake: a clip with no usable excerpt, or one
    Whisper cannot transcribe, still enrolls its timbre, and the note says
    what advanced cloning needs instead.
    """
    files = whisper.files()
    try:
        excerpt = reference.select_excerpt(audio_in.volume_normalize(wav))
        semantic = reference.semantic_tokens(excerpt)
        heard = whisper.transcribe(excerpt, files)
    except reference.NoUsableExcerpt as exc:
        return [], "", str(exc)
    except Exception:
        logger.exception("advanced reference preparation failed")
        return [], "", "The reference excerpt could not be prepared; see server logs."
    finally:
        # Both are large and needed once per voice, not once per line.
        reference.release()
        whisper.release(files)
    text = clean_reference_text(heard.text)
    if not heard.complete or not text or len(heard.text) > tokens.MAX_REFERENCE_TEXT:
        return semantic, "", "Whisper could not transcribe the excerpt. Play it and type what it says."
    return semantic, text, ""


async def enroll_upload(data: bytes, *, filename: str = "", with_reference: bool = False) -> Enrollment:
    """Enroll an uploaded audio file without blocking the event loop.

    With *with_reference*, the same decode also yields the advanced reference.
    """

    def run() -> Enrollment:
        wav = audio_in.decode(data, filename=filename)
        speaker = enroll.enroll_signal(enroll.reference_clip(wav))
        if not with_reference:
            return Enrollment(speaker)
        semantic, text, note = _prepare_reference(wav)
        return Enrollment(speaker, semantic, text, note)

    return await asyncio.to_thread(run)


async def reference_audio(reference_tokens: Sequence[int], speaker_tokens: Sequence[int]) -> tuple[bytes, int]:
    """The stored excerpt, decoded back to audio so its transcript can be checked.

    Rebuilt from its tokens rather than stored: Orb keeps no uploaded audio.
    """
    ok, reason = enrollment_ready(await get_settings())
    if not ok:
        raise service.SynthesisFailed(reason)
    excerpt = tokens.validate_reference_tokens(list(reference_tokens))
    speaker = tokens.validate_speaker_tokens(list(speaker_tokens))
    pcm = await asyncio.to_thread(codec.decode, excerpt, speaker)
    return pcm, codec.SAMPLE_RATE


async def synthesize(
    text: str,
    speaker_tokens: Sequence[int],
    settings: Mapping[str, Any],
    *,
    reference_tokens: Sequence[int] = (),
    reference_text: str = "",
) -> tuple[bytes, int]:
    """Speak *text* in an enrolled voice. Returns ``(pcm16, sample_rate)``.

    A reference with both tokens and a transcript makes this an advanced
    voice; either half alone is the basic one.
    """
    ok, reason = synthesis_ready(settings)
    if not ok:
        raise service.SynthesisFailed(reason)
    excerpt = reference.Reference(reference_text, reference_tokens) if reference_tokens and reference_text.strip() else None
    return await service.synthesize(text, speaker_tokens, reference=excerpt, gpu=use_gpu(settings))


class _LlmManagement:
    """Local ML route hooks for the llama-server half."""

    async def status_extra(self, settings: Mapping[str, Any]) -> dict:
        return {"gpu": use_gpu(settings), **service.state()}

    async def apply_config(self, body: Mapping[str, Any]) -> dict:
        gpu = bool(body.get("gpu", True))
        await set_local_ml_config(FEATURE_LLM, {"gpu": gpu})
        # Apply the new profile on the next synthesis.
        try:
            service.HOST.mark_stale(config.launch_profile(gpu=gpu))
        except RuntimeError:  # not downloaded yet: nothing to point at, and that is fine
            service.HOST.mark_stale(None)
        settings = await get_settings()
        return settings.get("local_ml_config", {})

    async def on_enabled(self, enabled: bool) -> None:
        if not enabled:
            await service.HOST.release()

    async def release_host(self) -> None:
        """Release the mmap and child process before deleting the GGUF."""
        await service.HOST.release()


LLM_MANAGEMENT = _LlmManagement()


__all__ = [
    "FEATURE_CODEC",
    "FEATURE_LLM",
    "FEATURE_REFERENCE",
    "FEATURE_SPEECH_RECOGNIZER",
    "LLM_MANAGEMENT",
    "Enrollment",
    "clean_reference_text",
    "clean_reference_tokens",
    "clean_tokens",
    "enroll_upload",
    "enrollment_ready",
    "reference_audio",
    "reference_ready",
    "synthesis_ready",
    "synthesize",
    "use_gpu",
]
