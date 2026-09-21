"""Synthesize speech blocks and align their text."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from ..toolkit import (
    markup_axes,
    spark_voice_clean_reference_text,
    spark_voice_clean_reference_tokens,
    spark_voice_clean_tokens,
    speech_input,
)
from .engine.base import SpeakableChunk
from .engine.regex_extractor import regex_extract
from .engine.router import get_adapter

#: This workflow's registry id, and the key its per-character profile is stored
#: under. Defined here rather than in ``hooks`` because the profile is this
#: module's contract and a caller outside the workflow — the voice-enrollment
#: route — needs the key without importing the hook surface.
WORKFLOW_ID = "tts"

# Field set of a per-character voice profile, stored in
# ``character_cards.workflow_state['tts']`` (read via get_workflow_character_state).
# Voice identity and credentials both live here; ``enabled`` gates automatic
# per-turn generation for the character.
PROFILE_DEFAULTS: dict = {
    "backend": "spark",
    "voice_id": "cloned",
    "language": "en-US",
    "rate": 1.0,
    "pitch": 1.0,
    "enabled": False,
    "api_url": "",
    "api_key": "",
    "model": "",
    # Built-in Spark-TTS voice data.
    "speaker_tokens": [],
    "speaker_ref_name": "",
    # Optional advanced-cloning reference.
    "clone_mode": "basic",
    "reference_tokens": [],
    "reference_text": "",
}

CLONE_MODES = ("basic", "advanced")

# Reproduction fields stored in attachment metadata.
_METADATA_KEYS = (
    "backend",
    "voice_id",
    "language",
    "rate",
    "pitch",
    "api_url",
    "api_key",
    "model",
    "speaker_tokens",
    "clone_mode",
    "reference_tokens",
    "reference_text",
)


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def normalize_profile(raw: object) -> dict:
    """Merge a stored/partial profile over ``PROFILE_DEFAULTS``.

    Missing or null fields fall back to defaults; ``rate``/``pitch`` are
    coerced to float so a value that round-tripped through JSON as a string
    still reaches the adapter as a number.
    """
    out = dict(PROFILE_DEFAULTS)
    if isinstance(raw, dict):
        for key in PROFILE_DEFAULTS:
            val = raw.get(key)
            if val is not None:
                out[key] = val
    out["rate"] = _as_float(out["rate"], 1.0)
    out["pitch"] = _as_float(out["pitch"], 1.0)
    out["enabled"] = bool(out["enabled"])
    # Invalid voice data is treated as no voice.
    out["speaker_tokens"] = spark_voice_clean_tokens(out.get("speaker_tokens"))
    out["speaker_ref_name"] = str(out.get("speaker_ref_name") or "")
    out["clone_mode"] = out["clone_mode"] if out["clone_mode"] in CLONE_MODES else "basic"
    out["reference_tokens"] = spark_voice_clean_reference_tokens(out.get("reference_tokens"))
    out["reference_text"] = spark_voice_clean_reference_text(out.get("reference_text"))
    return out


def speaks_advanced(profile: Mapping[str, Any]) -> bool:
    """Whether *profile* uses its advanced reference."""
    return bool(
        profile.get("backend") == "spark"
        and profile.get("clone_mode") == "advanced"
        and profile.get("reference_tokens")
        and profile.get("reference_text")
    )


def _voice_record(profile: Mapping[str, Any]) -> dict:
    """Return reproduction fields, including the reference only when used."""
    record = {k: profile.get(k, PROFILE_DEFAULTS.get(k, "")) for k in _METADATA_KEYS}
    if not speaks_advanced(profile):
        record.update(clone_mode="basic", reference_tokens=[], reference_text="")
    return record


# Local backends that stitch per-chunk clips together and emit WAV; every
# other backend returns MP3. `spark` is the built-in cloner and `spark_remote`
# the sidecar it replaced; both emit 16 kHz PCM.
_WAV_BACKENDS = frozenset({"kokoro", "spark", "spark_remote"})


def audio_mime_ext(backend: str) -> tuple[str, str]:
    """The MIME type and filename extension a backend emits."""
    if backend in _WAV_BACKENDS:
        return "audio/wav", "wav"
    return "audio/mpeg", "mp3"


def _estimate_mp3_duration_ms(audio: bytes) -> int:
    """Read an MP3's frame headers without decoding the encoded payload."""
    if len(audio) < 4:
        return 0
    offset = 0
    if audio[:3] == b"ID3" and len(audio) >= 10:
        tag_size = 0
        for value in audio[6:10]:
            tag_size = (tag_size << 7) | (value & 0x7F)
        offset = 10 + tag_size

    bitrates = {
        3: (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320),
        2: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
        0: (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160),
    }
    sample_rates = (44100, 48000, 32000)
    samples = 0
    sample_rate = 0
    frames = 0
    while offset + 4 <= len(audio):
        header = int.from_bytes(audio[offset : offset + 4], "big")
        if (header >> 21) & 0x7FF != 0x7FF:
            offset += 1
            continue
        version = (header >> 19) & 0x3
        layer = (header >> 17) & 0x3
        bitrate_index = (header >> 12) & 0xF
        sample_index = (header >> 10) & 0x3
        if version == 1 or layer != 1 or bitrate_index in (0, 15) or sample_index == 3:
            offset += 1
            continue
        bitrate = bitrates[version][bitrate_index] * 1000
        rate = sample_rates[sample_index]
        if version == 2:
            rate //= 2
        elif version == 0:
            rate //= 4
        padding = (header >> 9) & 1
        frame_length = ((144 if version == 3 else 72) * bitrate // rate) + padding
        if frame_length <= 0 or offset + frame_length > len(audio):
            break
        frames += 1
        samples += 1152 if version == 3 else 576
        sample_rate = rate
        offset += frame_length
    if not frames or not sample_rate:
        return 0
    return round(samples / sample_rate * 1000)


def estimate_audio_duration_ms(audio: bytes, content_type: str) -> int:
    """Return a cheap duration estimate for encoded formats used by TTS."""
    if content_type.lower().split(";", 1)[0].strip() == "audio/mpeg":
        return _estimate_mp3_duration_ms(audio)
    return 0


def compute_seed(text: str, profile: dict, blocks: list[dict] | None = None) -> str:
    """A deterministic fingerprint of the audio's identity (voice params +
    source text), used as the attachment ``seed``. Non-empty, so the row is
    rehydratable. The api_key is excluded -- a credential is not part of what
    the audio sounds like, and the seed is client-visible."""
    record = _voice_record(profile)
    basis = "|".join(str(record[k]) for k in _METADATA_KEYS if k != "api_key")
    if blocks is not None:
        basis += "|" + json.dumps([b.get("chunk") for b in blocks], sort_keys=True)
    return hashlib.md5((basis + "|" + text).encode("utf-8"), usedforsecurity=False).hexdigest()


def build_generation_metadata(text: str, profile: dict, blocks: list[dict] | None = None) -> dict:
    """The self-contained reproduction record stored on the attachment.

    Carries every parameter the synthesis call needs plus the source text, so
    reroll/rehydrate -- whose context has no character to read the profile
    from -- can reproduce the audio from this dict alone.
    """
    md = _voice_record(profile)
    md["text"] = text
    if blocks is not None:
        md["speech_chunks"] = [b["chunk"] for b in blocks]
    return md


def _backend_kwargs(profile: dict, settings: Mapping[str, Any] | None) -> dict:
    """Profile fields only some backends read, passed to all of them.

    Every adapter takes ``**kwargs``, so this stays one call shape rather than a
    branch per backend. ``speaker_tokens`` is the built-in Spark cloner's voice,
    and ``reference_tokens``/``reference_text`` its advanced reference, sent
    only when the profile speaks with it;
    ``settings`` is how a local backend reads its own Local ML gates, and stays
    ``None`` when the caller had none — preview and reroll both synthesize from
    a context that carries no settings, and making this path fetch them would
    put a database read in front of every remote backend that never looks.
    """
    advanced = speaks_advanced(profile)
    return {
        "speaker_tokens": list(profile.get("speaker_tokens") or []),
        "reference_tokens": list(profile.get("reference_tokens") or []) if advanced else [],
        "reference_text": str(profile.get("reference_text") or "") if advanced else "",
        "settings": settings,
    }


async def synthesize(text: str, profile: dict, *, settings: Mapping[str, Any] | None = None) -> tuple[bytes, str]:
    """Render ``text`` to audio under ``profile``. Returns ``(bytes, mime)``.

    Raises ``ValueError`` for an unknown backend (from ``get_adapter``) or
    when the backend produces no audio.
    """
    backend = profile.get("backend") or "spark"
    adapter = get_adapter(backend)
    # Preview is literal input, not an RP message needing dialogue discovery.
    spoken = " ".join(text.split())
    chunks = [SpeakableChunk(text=spoken, spoken_text=spoken)] if spoken else []
    result = await adapter.synthesize(
        chunks=chunks,
        voice_id=profile.get("voice_id") or PROFILE_DEFAULTS["voice_id"],
        language=profile.get("language") or PROFILE_DEFAULTS["language"],
        rate=_as_float(profile.get("rate"), 1.0),
        pitch=_as_float(profile.get("pitch"), 1.0),
        api_url=profile.get("api_url") or "",
        api_key=(profile.get("api_key") or None),
        model=profile.get("model") or "",
        **_backend_kwargs(profile, settings),
    )
    if not result.audio_bytes:
        raise ValueError("TTS synthesis produced no audio")
    return result.audio_bytes, result.content_type


def _alignment_key(token: str) -> str:
    """Workflow-private ASCII karaoke key, mirrored by the frontend plugin."""
    return "".join(char for char in token.lower() if char.isascii() and char.isalnum())


def _alignable_tokens(text: str) -> list[str]:
    """Whitespace tokens of ``text`` carrying at least one ASCII letter or digit.

    This is the word-alignment contract shared with the frontend karaoke mapper,
    which applies the same rule (lowercase, strip non-``[a-z0-9]``, drop empties).
    Punctuation-only and non-ASCII-only tokens are dropped on both sides, so the
    k-th span produced here lines up with the k-th highlighted on-screen word.
    """
    return [token for token in text.split() if _alignment_key(token)]


def estimate_word_spans(dialogue_text: str) -> list[dict]:
    """Char-proportional clip-relative spans, one per alignable token.

    Used when a backend reports no native word timing. The ms scale is nominal:
    the karaoke driver re-anchors every span set to the decoded clip duration, so
    only the relative widths matter (here, proportional to token length). Empty
    when the text has no alignable token.
    """
    tokens = _alignable_tokens(dialogue_text)
    spans: list[dict] = []
    cursor = 0.0
    for tok in tokens:
        width = float(len(tok))
        spans.append({"start_ms": cursor, "end_ms": cursor + width})
        cursor += width
    return spans


def reconcile_boundaries(dialogue_text: str, boundaries: list[dict]) -> list[dict] | None:
    """Map a backend's native word-boundary events onto the alignable tokens.

    ``boundaries`` is the backend's per-word timing stream
    (``[{text, start_ms, end_ms}]``); its tokenization need not match ours, so
    each boundary is located in ``dialogue_text`` by a forward character cursor
    and attributed to every alignable token whose character span it overlaps. A
    token covered by several boundaries takes their union. Returns ``None`` when
    any token receives no boundary -- the mapping is then incomplete and the
    caller estimates instead. When not ``None``, the result has one span per
    alignable token, in order.
    """
    tokens = _alignable_tokens(dialogue_text)
    if not tokens or not boundaries:
        return None

    token_spans: list[tuple[int, int]] = []
    cursor = 0
    for tok in tokens:
        idx = dialogue_text.find(tok, cursor)
        if idx < 0:
            return None
        token_spans.append((idx, idx + len(tok)))
        cursor = idx + len(tok)

    starts: list[float | None] = [None] * len(tokens)
    ends: list[float | None] = [None] * len(tokens)
    bcursor = 0
    lo = 0
    for b in boundaries:
        btext = b.get("text") or ""
        start_ms = b.get("start_ms")
        end_ms = b.get("end_ms")
        if not btext or start_ms is None or end_ms is None:
            continue
        pos = dialogue_text.find(btext, bcursor)
        if pos < 0:
            continue
        bcursor = pos + len(btext)
        b_start, b_end = pos, pos + len(btext)
        while lo < len(tokens) and token_spans[lo][1] <= b_start:
            lo += 1
        j = lo
        while j < len(tokens) and token_spans[j][0] < b_end:
            if starts[j] is None or start_ms < starts[j]:
                starts[j] = start_ms
            if ends[j] is None or end_ms > ends[j]:
                ends[j] = end_ms
            j += 1

    spans: list[dict] = []
    for s, e in zip(starts, ends):
        if s is None or e is None:
            return None
        spans.append({"start_ms": float(s), "end_ms": float(e)})
    return spans


async def synthesize_blocks(
    text: str,
    profile: dict,
    *,
    settings: Mapping[str, Any] | None = None,
    speech_chunks: Sequence[dict] | None = None,
    legacy: bool = False,
) -> tuple[bytes, str, list[dict]]:
    """Render each speakable block as a self-contained clip."""
    backend = profile.get("backend") or "spark"
    adapter = get_adapter(backend)
    if speech_chunks is not None:
        chunks = [SpeakableChunk(**chunk) for chunk in speech_chunks]
    else:
        prepared = text if legacy else speech_input(text)
        style = await markup_axes(prepared, settings) if settings is not None and not legacy else None
        chunks = regex_extract(
            text=prepared,
            backend_type=backend,
            supports_emotion_tags=adapter.supports_emotion_tags,
            style=style,
            legacy=legacy,
            input_prepared=not legacy,
        )
    pause_after = [chunks[i + 1].pause_before_ms if i + 1 < len(chunks) else 0 for i in range(len(chunks))]
    voice_id = profile.get("voice_id") or PROFILE_DEFAULTS["voice_id"]
    language = profile.get("language") or PROFILE_DEFAULTS["language"]
    rate = _as_float(profile.get("rate"), 1.0)
    pitch = _as_float(profile.get("pitch"), 1.0)
    api_url = profile.get("api_url") or ""
    api_key = profile.get("api_key") or None
    model = profile.get("model") or ""
    extra = _backend_kwargs(profile, settings)

    parts: list[bytes] = []
    blocks: list[dict] = []
    offset = 0
    for i, chunk in enumerate(chunks):
        recorded_chunk = asdict(chunk)
        chunk.pause_before_ms = 0
        chunk.pause_after_ms = 0
        result = await adapter.synthesize(
            chunks=[chunk],
            voice_id=voice_id,
            language=language,
            rate=rate,
            pitch=pitch,
            api_url=api_url,
            api_key=api_key,
            model=model,
            **extra,
        )
        clip = result.audio_bytes or b""
        parts.append(clip)
        spoken = chunk.spoken_text or chunk.text
        words = reconcile_boundaries(spoken, result.word_boundaries) if result.word_boundaries else None
        if words is None:
            words = estimate_word_spans(spoken)
        duration_ms = max(
            int(result.duration_ms or 0),
            max((int(word.get("end_ms") or 0) for word in (result.word_boundaries or [])), default=0),
            estimate_audio_duration_ms(clip, result.content_type),
        )
        blocks.append(
            {
                "spoken_text": spoken,
                "chunk": recorded_chunk,
                "byte_start": offset,
                "byte_end": offset + len(clip),
                "duration_ms": duration_ms,
                "pause_after_ms": pause_after[i],
                "words": words,
            }
        )
        offset += len(clip)
    if offset == 0:
        raise ValueError("TTS synthesis produced no audio")
    # All clips share the backend's format, so one mime describes the row.
    return b"".join(parts), audio_mime_ext(backend)[0], blocks


async def synthesize_blocks_from_metadata(metadata: dict) -> tuple[bytes, str, list[dict]]:
    """Reproduce per-block audio from a stored ``generation_metadata`` dict.

    Backs the reroll and rehydrate hooks, whose context carries no character
    profile -- the metadata is the sole input. Raises ``ValueError`` when the
    record lacks the source text.
    """
    text = metadata.get("text") if isinstance(metadata, dict) else None
    if not text:
        raise ValueError("generation_metadata carries no text to synthesize")
    return await synthesize_blocks(
        text, normalize_profile(metadata), speech_chunks=metadata.get("speech_chunks"), legacy="speech_chunks" not in metadata
    )


def consumption_blocks(blocks: list[dict]) -> list[dict]:
    """Public playback metadata excludes the synthesis reproduction record."""
    return [{key: value for key, value in block.items() if key != "chunk"} for block in blocks]
