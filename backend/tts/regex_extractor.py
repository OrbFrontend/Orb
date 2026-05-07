"""
backend/tts/regex_extractor.py — Algorithmic dialogue extractor.

Extracts speakable dialogue from RP text using regex/heuristics.
Zero LLM calls, zero latency, zero cost. This is the only
extraction path in this PR; model-backed expressive extraction is deferred.

Pattern handling:
- Quoted dialogue ("Hello") → extract
- Action beats in asterisks (*she laughs*) → pause or skip
- Thoughts in parentheses (hmm...) → skip entirely
- Narrator/scene text between dialogue → skip
- Emotion inferred from punctuation and nearby action beats
"""

from __future__ import annotations

import re

from .base import SpeakableChunk

# ---------------------------------------------------------------------------
# Audible vs silent action beats
# ---------------------------------------------------------------------------

# Actions that produce sound → convert to pause + optional tag
AUDIBLE_BEATS = frozenset(
    {
        "laughs",
        "laugh",
        "giggles",
        "chuckles",
        "chuckle",
        "sighs",
        "sigh",
        "gasps",
        "gasp",
        "moans",
        "moan",
        "groans",
        "groan",
        "sniffles",
        "sniffle",
        "coughs",
        "cough",
        "cries",
        "cry",
        "sobs",
        "sob",
        "whimpers",
        "whimper",
        "screams",
        "scream",
        "shouts",
        "shout",
        "whispers",
        "whisper",
        "mutters",
        "mutter",
        "murmurs",
        "murmur",
        "hums",
        "hum",
        "hisses",
        "hiss",
        "growls",
        "growl",
        "pants",
        "pant",
        "breathes",
        "breathe",
        "snorts",
        "snort",
    }
)

# Tags for backends that support them (ElevenLabs, Fish Speech, etc.)
AUDIBLE_TAG_MAP = {
    "laughs": "[laugh]",
    "laugh": "[laugh]",
    "giggles": "[laugh]",
    "giggle": "[laugh]",
    "chuckles": "[chuckle]",
    "chuckle": "[chuckle]",
    "sighs": "[sigh]",
    "sigh": "[sigh]",
    "gasps": "[gasp]",
    "gasp": "[gasp]",
    "moans": "[moan]",
    "moan": "[moan]",
    "groans": "[groan]",
    "groan": "[groan]",
    "coughs": "[cough]",
    "cough": "[cough]",
    "screams": "[scream]",
    "scream": "[scream]",
    "shouts": "[scream]",
    "shout": "[scream]",
    "whispers": "[whisper]",
    "whisper": "[whisper]",
    "hisses": "[hiss]",
    "hiss": "[hiss]",
    "growls": "[growl]",
    "growl": "[growl]",
}

# Emotions inferred from audible action beats
AUDIBLE_EMOTION_MAP = {
    "laughs": "amused",
    "laugh": "amused",
    "giggles": "amused",
    "giggle": "amused",
    "chuckles": "amused",
    "chuckle": "amused",
    "sighs": "soft",
    "sigh": "soft",
    "gasps": "surprised",
    "gasp": "surprised",
    "moans": "soft",
    "moan": "soft",
    "groans": "angry",
    "groan": "angry",
    "cries": "sad",
    "cry": "sad",
    "sobs": "sad",
    "sob": "sad",
    "whimpers": "fearful",
    "whimper": "fearful",
    "screams": "fearful",
    "scream": "fearful",
    "shouts": "angry",
    "shout": "angry",
    "whispers": "whispered",
    "whisper": "whispered",
    "mutters": "angry",
    "mutter": "angry",
    "murmurs": "soft",
    "murmur": "soft",
    "hisses": "angry",
    "hiss": "angry",
    "growls": "angry",
    "growl": "angry",
    "pants": "breathless",
    "pant": "breathless",
}


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

# Matches action beats in asterisks: *she laughs softly*
RE_ASTERISK = re.compile(r"\*([^*]+)\*")

# Matches thoughts in parentheses: (maybe I should...)
# Only match if the parens are NOT wrapping quoted dialogue
RE_PARENTHETICAL = re.compile(r"\(([^)]+)\)")

# Matches double-quoted dialogue: "Hello there"
RE_QUOTED = re.compile(r'"([^"]+)"')

# Matches em-dash dialogue (some RP styles): —Hello.—
RE_EMDASH = re.compile(r"\u2014([^\u2014]+?)\u2014")


# ---------------------------------------------------------------------------
# Emotion heuristics
# ---------------------------------------------------------------------------


def _infer_emotion(text: str) -> str:
    """Guess emotion from punctuation and text patterns."""
    raw = text.rstrip(" '\"")
    if raw.endswith("?!") or raw.endswith("!?"):
        return "surprised"
    if raw.endswith("!!"):
        return "angry"
    if raw.endswith("!"):
        return "warm"
    if raw.endswith("..."):
        return "soft"
    stripped = raw.rstrip(".!?")
    if stripped.isupper() and len(stripped) > 3:
        return "angry"

    return "neutral"


def _extract_beat_action(beat_text: str) -> str:
    """Extract the main action verb from an action beat.

    *she laughs softly* → 'laughs'
    *laughs* → 'laughs'
    """
    # Strip common prefixes: "she ", "he ", "they ", etc.
    words = beat_text.strip().lower().split()
    for _, w in enumerate(words):
        if w in AUDIBLE_BEATS or w in AUDIBLE_EMOTION_MAP:
            return w
    # Check if any word is a known beat
    for w in words:
        # Handle conjugations: stripping 's' or 'ed'
        if w.rstrip("s") in AUDIBLE_BEATS:
            return w.rstrip("s")
        if w.rstrip("ed") in AUDIBLE_BEATS:
            return w.rstrip("ed")
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def regex_extract(
    text: str,
    backend_type: str = "edge",
    supports_emotion_tags: bool = False,
) -> list[SpeakableChunk]:
    """Extract speakable dialogue from RP text using regex/heuristics.

    Args:
        text: Raw RP message text (writer output).
        backend_type: TTS backend name (for tag/emotion decisions).
        supports_emotion_tags: Whether the backend supports inline tags
            like [laugh], [sigh]. If False, audible beats become pauses.

    Returns:
        List of SpeakableChunks ready for TTS synthesis.
    """
    if not text or not text.strip():
        return []

    # Remove parenthetical thoughts entirely (inner monologue)
    cleaned = RE_PARENTHETICAL.sub("", text)

    # Build a segment list by tokenizing the text into beats and text
    # using the original positions (no string mutation)
    segments = []  # list of (type, data) — type is "beat" or "text"
    pos = 0

    for m in RE_ASTERISK.finditer(cleaned):
        # Text before this beat
        if m.start() > pos:
            segments.append(("text", cleaned[pos : m.start()]))
        # The beat itself
        action = _extract_beat_action(m.group(1))
        is_audible = action in AUDIBLE_BEATS or action in AUDIBLE_EMOTION_MAP
        segments.append(
            (
                "beat",
                {
                    "action": action,
                    "is_audible": is_audible,
                    "emotion": AUDIBLE_EMOTION_MAP.get(action, ""),
                    "tag": AUDIBLE_TAG_MAP.get(action, ""),
                },
            )
        )
        pos = m.end()
    # Remaining text after last beat
    if pos < len(cleaned):
        segments.append(("text", cleaned[pos:]))

    if not segments:
        return []

    # Now extract quoted dialogue from text segments, interleaving beat info
    chunks = []
    last_beat = None  # Most recent beat before the next dialogue

    for seg_type, seg_data in segments:
        if seg_type == "beat":
            last_beat = seg_data
            continue

        # Extract quoted dialogue from this text segment
        dialogue_matches = list(RE_QUOTED.finditer(seg_data))
        if not dialogue_matches:
            dialogue_matches = list(RE_EMDASH.finditer(seg_data))
        if not dialogue_matches:
            last_beat = None
            continue

        for dm in dialogue_matches:
            dialogue_text = dm.group(1).strip()
            if not dialogue_text:
                continue

            # Determine pause and emotion from preceding beat
            pause_before = 0
            beat_emotion = ""
            beat_tag = ""

            if last_beat:
                if last_beat["is_audible"]:
                    pause_before = 400
                    beat_emotion = last_beat.get("emotion", "")
                    if supports_emotion_tags and last_beat["tag"]:
                        beat_tag = last_beat["tag"]
                else:
                    # Silent action (smiles, shrugs, etc.)
                    pause_before = 200
                last_beat = None  # Consume the beat

            # Add inter-dialogue pause if not the first chunk
            if chunks and pause_before == 0:
                pause_before = 300

            # Determine emotion from text + beat context
            emotion = _infer_emotion(dialogue_text)
            if beat_emotion and emotion == "neutral":
                emotion = beat_emotion

            # Build final text with optional tag prefix
            final_text = dialogue_text
            if beat_tag:
                final_text = f"{beat_tag} {dialogue_text}"

            chunks.append(
                SpeakableChunk(
                    text=final_text,
                    emotion=emotion,
                    pause_before_ms=pause_before,
                    pause_after_ms=0,
                )
            )

    if not chunks:
        return []

    # First chunk should not have a leading pause
    chunks[0].pause_before_ms = 0
    return chunks
