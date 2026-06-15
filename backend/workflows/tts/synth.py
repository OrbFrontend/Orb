"""Synthesis logic for the TTS workflow: profile normalization, the
text-to-bytes call, and the reproduction record (seed + generation metadata).

No conversation, turn, or HTTP state is touched here -- every function maps
its arguments to a value (the synthesis call reaches a TTS backend over the
network through an engine adapter, but takes no orchestration context). The
hook layer in ``hooks.py`` wires these into the pipeline and HTTP routes.
"""

from __future__ import annotations

import hashlib

from .engine.regex_extractor import regex_extract
from .engine.router import get_adapter

# Field set of a per-character voice profile, stored in
# ``character_cards.workflow_state['tts']`` (read via get_workflow_character_state).
# Voice identity and credentials both live here; ``enabled`` gates automatic
# per-turn generation for the character.
PROFILE_DEFAULTS: dict = {
    "backend": "edge",
    "voice_id": "en-US-JennyNeural",
    "language": "en-US",
    "rate": 1.0,
    "pitch": 1.0,
    "enabled": False,
    "api_url": "",
    "api_key": "",
    "model": "",
}

# Reproduction-record keys carried in an attachment's generation_metadata.
# These plus the source text are sufficient to re-synthesize the identical
# audio from a context that has no character or turn state (reroll, rehydrate).
_METADATA_KEYS = ("backend", "voice_id", "language", "rate", "pitch", "api_url", "api_key", "model")


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
    return out


def audio_mime_ext(backend: str) -> tuple[str, str]:
    """The MIME type and filename extension a backend emits. Kokoro returns
    WAV; every other backend returns MP3."""
    if backend == "kokoro":
        return "audio/wav", "wav"
    return "audio/mpeg", "mp3"


def compute_seed(text: str, profile: dict) -> str:
    """A deterministic fingerprint of the audio's identity (voice params +
    source text), used as the attachment ``seed``. Non-empty, so the row is
    rehydratable. The api_key is excluded -- a credential is not part of what
    the audio sounds like, and the seed is client-visible."""
    basis = "|".join(str(profile.get(k, "")) for k in _METADATA_KEYS if k != "api_key")
    return hashlib.md5((basis + "|" + text).encode("utf-8"), usedforsecurity=False).hexdigest()


def build_generation_metadata(text: str, profile: dict) -> dict:
    """The self-contained reproduction record stored on the attachment.

    Carries every parameter the synthesis call needs plus the source text, so
    reroll/rehydrate -- whose context has no character to read the profile
    from -- can reproduce the audio from this dict alone.
    """
    md = {k: profile.get(k, PROFILE_DEFAULTS.get(k, "")) for k in _METADATA_KEYS}
    md["text"] = text
    return md


async def synthesize(text: str, profile: dict) -> tuple[bytes, str]:
    """Render ``text`` to audio under ``profile``. Returns ``(bytes, mime)``.

    Raises ``ValueError`` for an unknown backend (from ``get_adapter``) or
    when the backend produces no audio.
    """
    backend = profile.get("backend") or "edge"
    adapter = get_adapter(backend)
    chunks = regex_extract(
        text=text,
        backend_type=backend,
        supports_emotion_tags=adapter.supports_emotion_tags,
    )
    result = await adapter.synthesize(
        chunks=chunks,
        voice_id=profile.get("voice_id") or PROFILE_DEFAULTS["voice_id"],
        language=profile.get("language") or PROFILE_DEFAULTS["language"],
        rate=_as_float(profile.get("rate"), 1.0),
        pitch=_as_float(profile.get("pitch"), 1.0),
        api_url=profile.get("api_url") or "",
        api_key=(profile.get("api_key") or None),
        model=profile.get("model") or "",
    )
    if not result.audio_bytes:
        raise ValueError("TTS synthesis produced no audio")
    return result.audio_bytes, result.content_type


def _alignable_tokens(text: str) -> list[str]:
    """Whitespace tokens of ``text`` carrying at least one ASCII letter or digit.

    This is the word-alignment contract shared with the frontend karaoke mapper,
    which applies the same rule (lowercase, strip non-``[a-z0-9]``, drop empties).
    Punctuation-only and non-ASCII-only tokens are dropped on both sides, so the
    k-th span produced here lines up with the k-th highlighted on-screen word.
    """
    return [t for t in text.split() if any(c.isascii() and c.isalnum() for c in t)]


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


async def synthesize_blocks(text: str, profile: dict) -> tuple[bytes, str, list[dict]]:
    """Render ``text`` as one self-contained clip per speakable block.

    Returns ``(concatenated_bytes, mime, blocks)`` where the bytes are
    ``clip0 ++ clip1 ++ ...`` -- each clip a complete file synthesized from a
    single ``regex_extract`` chunk -- and ``blocks[i]`` carries that clip's
    ``[byte_start, byte_end)``, the silence to insert after it, and ``words``:
    one clip-relative timing span per alignable word, which the frontend
    karaoke highlighter maps onto the rendered text. The frontend slices a clip
    out by byte range to play one block.

    The extractor expresses inter-chunk timing as a *leading* ``pause_before_ms``
    on the following chunk. That gap is relocated onto the previous block's
    ``pause_after_ms`` and zeroed on the chunk before synthesis, so the silence
    is reproduced by the player instead of being baked into a clip -- baking it
    in would both shift the clip's byte range and double the pause once the
    player adds its own gap. Raises ``ValueError`` when no audio is produced
    (empty text, no quoted dialogue, or every clip empty), matching
    ``synthesize`` so the hook callers degrade through their existing guards.
    """
    backend = profile.get("backend") or "edge"
    adapter = get_adapter(backend)
    chunks = regex_extract(
        text=text,
        backend_type=backend,
        supports_emotion_tags=adapter.supports_emotion_tags,
    )
    pause_after = [chunks[i + 1].pause_before_ms if i + 1 < len(chunks) else 0 for i in range(len(chunks))]
    voice_id = profile.get("voice_id") or PROFILE_DEFAULTS["voice_id"]
    language = profile.get("language") or PROFILE_DEFAULTS["language"]
    rate = _as_float(profile.get("rate"), 1.0)
    pitch = _as_float(profile.get("pitch"), 1.0)
    api_url = profile.get("api_url") or ""
    api_key = profile.get("api_key") or None
    model = profile.get("model") or ""

    parts: list[bytes] = []
    blocks: list[dict] = []
    offset = 0
    for i, chunk in enumerate(chunks):
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
        )
        clip = result.audio_bytes or b""
        parts.append(clip)
        spoken = chunk.spoken_text or chunk.text
        words = reconcile_boundaries(spoken, result.word_boundaries) if result.word_boundaries else None
        if words is None:
            words = estimate_word_spans(spoken)
        blocks.append(
            {
                "byte_start": offset,
                "byte_end": offset + len(clip),
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
    return await synthesize_blocks(text, normalize_profile(metadata))
