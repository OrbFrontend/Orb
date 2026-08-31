"""Extract dialogue and narration for speech synthesis."""

from __future__ import annotations

from .base import SpeakableChunk

# Audible vs silent action beats

# Actions that produce sound → convert to pause + optional tag. The compact
# string keeps this public set byte-for-byte stable; effect aliases below also
# include ``giggle``, which was historically recognized through the emotion map.
AUDIBLE_BEATS = frozenset(
    """laughs laugh giggles chuckles chuckle sighs sigh gasps gasp moans moan
    groans groan sniffles sniffle coughs cough cries cry sobs sob whimpers whimper
    screams scream shouts shout whispers whisper mutters mutter murmurs murmur hums
    hum hisses hiss growls growl pants pant breathes breathe snorts snort""".split()
)

# Aliases share one source of truth for backend tags and inferred emotions.
# Empty fields mean that an audible beat has no corresponding effect.
_BEAT_EFFECTS = (
    (("laughs", "laugh", "giggles", "giggle"), "[laugh]", "amused"),
    (("chuckles", "chuckle"), "[chuckle]", "amused"),
    (("sighs", "sigh"), "[sigh]", "soft"),
    (("gasps", "gasp"), "[gasp]", "surprised"),
    (("moans", "moan"), "[moan]", "soft"),
    (("groans", "groan"), "[groan]", "angry"),
    (("sniffles", "sniffle"), "", ""),
    (("coughs", "cough"), "[cough]", ""),
    (("cries", "cry", "sobs", "sob"), "", "sad"),
    (("whimpers", "whimper"), "", "fearful"),
    (("screams", "scream"), "[scream]", "fearful"),
    (("shouts", "shout"), "[scream]", "angry"),
    (("whispers", "whisper"), "[whisper]", "whispered"),
    (("mutters", "mutter"), "", "angry"),
    (("murmurs", "murmur"), "", "soft"),
    (("hums", "hum"), "", ""),
    (("hisses", "hiss"), "[hiss]", "angry"),
    (("growls", "growl"), "[growl]", "angry"),
    (("pants", "pant"), "", "breathless"),
    (("breathes", "breathe", "snorts", "snort"), "", ""),
)
AUDIBLE_TAG_MAP = {alias: tag for aliases, tag, _ in _BEAT_EFFECTS if tag for alias in aliases}
AUDIBLE_EMOTION_MAP = {alias: emotion for aliases, _, emotion in _BEAT_EFFECTS if emotion for alias in aliases}


# Workflow-private markup scanner

# This workflow intentionally owns its parser instead of importing application
# lexical utilities. Keep its frontend twin independent too; shared adversarial
# fixtures enforce behavior across the Python/JavaScript boundary.
_QUOTE_PAIRS = {
    "“": "”",
    "‘": "’",
    "«": "»",
    "‹": "›",
    "「": "」",
    "『": "』",
    "„": "“",
    "‚": "‘",
}
_OPEN_QUOTES = frozenset(_QUOTE_PAIRS)
_CLOSE_QUOTES = frozenset(_QUOTE_PAIRS.values())
_HARD_BREAKS = frozenset("\n\v\f\r\x1c\x1d\x1e\x85\u2028\u2029")


def _escaped(text: str, index: int) -> bool:
    slashes = 0
    index -= 1
    while index >= 0 and text[index] == "\\":
        slashes += 1
        index -= 1
    return bool(slashes % 2)


def _find_quoted_spans(text: str) -> list[tuple[int, int, int, int]]:
    """Outer balanced quotes as ``(start, end, content_start, content_end)``."""
    spans: list[tuple[int, int, int, int]] = []
    stack: list[str] = []
    outer_start = 0
    for index, char in enumerate(text):
        if char == '"' and _escaped(text, index):
            continue
        if char == "’" and 0 < index < len(text) - 1 and text[index - 1].isalnum() and text[index + 1].isalnum():
            continue
        if char == '"' and not stack and index > 0 and text[index - 1].isdigit():
            continue

        if stack and char == stack[-1]:
            stack.pop()
            if not stack:
                spans.append((outer_start, index + 1, outer_start + 1, index))
            continue
        if char == '"':
            if not stack:
                outer_start = index
            stack.append(char)
        elif char in _OPEN_QUOTES:
            if not stack:
                outer_start = index
            stack.append(_QUOTE_PAIRS[char])
        elif char in _CLOSE_QUOTES and stack:
            if char in stack:
                while stack:
                    if stack.pop() == char:
                        break
            if not stack:
                spans.append((outer_start, index + 1, outer_start + 1, index))
    return spans


def _overlaps(start: int, end: int, spans: list[tuple[int, ...]]) -> bool:
    return any(start < span[1] and span[0] < end for span in spans)


def _find_parenthetical_spans(text: str, quoted: list[tuple[int, ...]]) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for index, char in enumerate(text):
        if _overlaps(index, index + 1, quoted):
            continue
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            start = stack.pop()
            if not stack:
                spans.append((start, index + 1))
    return spans


def _find_beat_spans(
    text: str, quoted: list[tuple[int, ...]], parentheticals: list[tuple[int, ...]]
) -> list[tuple[int, int, int, int]]:
    spans: list[tuple[int, int, int, int]] = []
    opener: int | None = None
    for index, char in enumerate(text):
        if char != "*" or _escaped(text, index) or _overlaps(index, index + 1, quoted + parentheticals):
            continue
        if (index > 0 and text[index - 1] == "*") or (index + 1 < len(text) and text[index + 1] == "*"):
            continue
        if opener is None:
            if index + 1 < len(text) and not text[index + 1].isspace() and text[index + 1] not in _HARD_BREAKS:
                opener = index
        elif index > opener + 1 and not text[index - 1].isspace():
            spans.append((opener, index + 1, opener + 1, index))
            opener = None
    return spans


def _find_emdash_spans(text: str, protected: list[tuple[int, ...]]) -> list[tuple[int, int, int, int]]:
    spans: list[tuple[int, int, int, int]] = []
    opener: int | None = None
    for index, char in enumerate(text):
        if char != "—" or _overlaps(index, index + 1, protected):
            continue
        if opener is None:
            opener = index
        else:
            if index > opener + 1:
                spans.append((opener, index + 1, opener + 1, index))
            opener = None
    return spans


def _spoken_text(text: str) -> str:
    """Normalize whitespace so a speakable block never contains a line break."""
    return " ".join(text.split())


# Emotion heuristics


def _infer_emotion(text: str) -> str:
    """Guess emotion from punctuation and text patterns."""
    raw = text.rstrip(" '\"”’»›」』")
    if raw.endswith(("?!", "!?", "？！", "！？", "؟!", "!؟")):
        return "surprised"
    if raw.endswith(("!!", "！！")):
        return "angry"
    if raw.endswith(("!", "！")):
        return "warm"
    if raw.endswith(("...", "…")):
        return "soft"
    stripped = raw.rstrip(".!?…。！？؟۔｡．।॥")
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


# Public API


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

    quoted = _find_quoted_spans(text)
    parentheticals = _find_parenthetical_spans(text, quoted)
    beats = _find_beat_spans(text, quoted, parentheticals)
    emdashes = _find_emdash_spans(text, quoted + parentheticals + beats)
    dialogues = [span for span in quoted + emdashes if not _overlaps(span[0], span[1], parentheticals)]

    events: list[tuple[int, str, tuple[int, int, int, int]]] = [
        *((span[0], "beat", span) for span in beats),
        *((span[0], "dialogue", span) for span in dialogues),
    ]
    events.sort(key=lambda item: (item[0], item[1] != "beat"))

    chunks: list[SpeakableChunk] = []
    last_beat = None  # Most recent beat before the next dialogue
    for _start, event_type, span in events:
        if event_type == "beat":
            action = _extract_beat_action(text[span[2] : span[3]])
            last_beat = {
                "action": action,
                "is_audible": action in AUDIBLE_BEATS or action in AUDIBLE_EMOTION_MAP,
                "emotion": AUDIBLE_EMOTION_MAP.get(action, ""),
                "tag": AUDIBLE_TAG_MAP.get(action, ""),
            }
            continue

        dialogue_text = _spoken_text(text[span[2] : span[3]])
        if not dialogue_text:
            continue

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
                pause_before = 200
            last_beat = None
        if chunks and pause_before == 0:
            pause_before = 300

        emotion = _infer_emotion(dialogue_text)
        if beat_emotion and emotion == "neutral":
            emotion = beat_emotion
        final_text = f"{beat_tag} {dialogue_text}" if beat_tag else dialogue_text
        chunks.append(
            SpeakableChunk(
                text=final_text,
                spoken_text=dialogue_text,
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
