"""Extract dialogue and narration for speech synthesis."""

from __future__ import annotations

from dataclasses import dataclass

from ...toolkit import AxisStyle, speech_segments, split_sentence_units
from .base import SpeakableChunk

# Audible vs silent action beats

# Actions that produce sound -> convert to pause + optional tag. The compact string keeps this public set byte-for-byte stable;
# effect aliases below also include ``giggle``, which was historically recognized through the emotion map.
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

# Frozen for legacy attachment replay and its frontend extraction fallback.
# New speech uses the host analysis exposed through the workflow toolkit.
_QUOTE_PAIRS = {"“": "”", "‘": "’", "«": "»", "‹": "›", "「": "」", "『": "』", "„": "“", "‚": "‘"}
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


# Narration pacing

# Narration is never read aloud, so the gap before the next line stands in for it. Hyperbolic in its length, so _GAP_MAX_MS is an
# asymptote rather than a plateau every long beat flattens onto.
_GAP_MIN_MS = 300
_GAP_MAX_MS = 3000
GAP_HALF_WORDS = 10  # narration length spending half the range above the floor
_UNVOICED_BEAT_MS = 200  # room for a sound the backend cannot perform
TAG_BEAT_WORDS = 3  # words a performed tag stands in for
_CHARS_PER_WORD = 6  # sizes a script that spaces no words, where tokens read as one


def _narration_words(text: str) -> int:
    content = (sum(1 for char in token if char.isalnum()) for token in text.split())
    return sum(max(1, count // _CHARS_PER_WORD) for count in content if count)


def _narration_pause_ms(words: int, *, unvoiced_beat: bool = False) -> int:
    """Silence standing in for ``words`` of narration between two spoken lines."""
    words = max(0, words)
    pause = _GAP_MIN_MS + round((_GAP_MAX_MS - _GAP_MIN_MS) * words / (words + GAP_HALF_WORDS))
    return min(pause + (_UNVOICED_BEAT_MS if unvoiced_beat else 0), _GAP_MAX_MS)


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

    *she laughs softly* -> 'laughs' *laughs* -> 'laughs'
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


def _bare_words(text: str) -> str:
    """*text* with punctuation trimmed from each word, so ``whispers.`` reads as ``whispers``."""
    return " ".join(word.strip("".join(ch for ch in word if not ch.isalnum())) for word in text.split())


# Compatibility scanner for attachments created before shared segmentation.


def _legacy_segments(text: str) -> list[tuple[str, str]]:
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

    return [(kind, text[span[2] : span[3]]) for _, kind, span in events]


# Public API


@dataclass
class SpokenLine:
    """What surrounds one spoken line in the source text; never stored with the chunk."""

    text: str
    lead: str = ""  # the narration sentence leading into the line, within its paragraph
    trail: str = ""  # the narration after the line up to its first sentence end
    beat_emotion: str = ""  # AUDIBLE_EMOTION_MAP value of the audible beat before the line
    lead_in: str = ""  # lead, or the previous paragraph's closing narration when the line opens its paragraph

    @property
    def window(self) -> str:
        return " ".join(part for part in (self.lead, f'"{self.text}"', self.trail) if part)


def _sentences(parts: list[str]) -> list[str]:
    """The sentences of *parts* joined; ``[""]`` when there are none."""
    return split_sentence_units(" ".join(" ".join(parts).split())) or [""]


def regex_extract(
    text: str,
    supports_emotion_tags: bool = False,
    *,
    style: AxisStyle | None = None,
    legacy: bool = False,
    input_prepared: bool = False,
) -> list[SpeakableChunk]:
    """Extract RP speech into SpeakableChunks with backend-specific emotion handling.

    Without emotion-tag support, audible beats become pauses. input_prepared
    requires the classified speech_input; legacy replay always reads original text.
    """
    chunks, _ = regex_extract_lines(text, supports_emotion_tags, style=style, legacy=legacy, input_prepared=input_prepared)
    return chunks


def regex_extract_lines(
    text: str,
    supports_emotion_tags: bool = False,
    *,
    style: AxisStyle | None = None,
    legacy: bool = False,
    input_prepared: bool = False,
) -> tuple[list[SpeakableChunk], list[SpokenLine]]:
    """``regex_extract`` plus one ``SpokenLine`` per chunk, in the same order."""
    if not text or not text.strip():
        return [], []

    if legacy:
        events = _legacy_segments(text)
    else:
        events = speech_segments(text, style, input_prepared=input_prepared, include_narration=True, paragraph_breaks=True)

    chunks: list[SpeakableChunk] = []
    lines: list[SpokenLine] = []
    between: list[str] = []  # narration and beats since the previous spoken line, within the paragraph
    open_line: SpokenLine | None = None  # the line still collecting its trailing narration
    paragraph_tail = ""  # closing narration sentence of the previous paragraph, until a line is spoken
    last_beat = None  # Most recent beat before the next dialogue
    gap_words = 0  # Narration, beats included, since the previous spoken line
    for event_type, segment_text in events:
        if event_type == "paragraph":
            if open_line:
                open_line.trail = _sentences(between)[0]
            paragraph_tail = _sentences(between)[-1] if between else paragraph_tail
            open_line = None
            between = []
            continue
        if event_type == "narration":
            between.append(segment_text)
            gap_words += _narration_words(segment_text)
            continue
        if event_type == "beat":
            between.append(segment_text)
            action = _extract_beat_action(segment_text if legacy else _bare_words(segment_text))
            last_beat = {
                "action": action,
                "is_audible": action in AUDIBLE_BEATS or action in AUDIBLE_EMOTION_MAP,
                "emotion": AUDIBLE_EMOTION_MAP.get(action, ""),
                "tag": AUDIBLE_TAG_MAP.get(action, ""),
                "words": _narration_words(segment_text),
            }
            gap_words += last_beat["words"]
            continue

        dialogue_text = _spoken_text(segment_text)
        if not dialogue_text:
            continue

        beat_emotion = ""
        beat_tag = ""
        unvoiced_beat = False
        if last_beat:
            if last_beat["is_audible"]:
                beat_emotion = last_beat.get("emotion", "")
                if supports_emotion_tags and last_beat["tag"]:
                    beat_tag = last_beat["tag"]
                    gap_words -= min(last_beat["words"], TAG_BEAT_WORDS)
                else:
                    unvoiced_beat = True
            last_beat = None
        pause_before = _narration_pause_ms(gap_words, unvoiced_beat=unvoiced_beat)
        gap_words = 0

        emotion = _infer_emotion(dialogue_text)
        if beat_emotion and emotion == "neutral":
            emotion = beat_emotion
        final_text = f"{beat_tag} {dialogue_text}" if beat_tag else dialogue_text
        chunks.append(
            SpeakableChunk(
                text=final_text, spoken_text=dialogue_text, emotion=emotion, pause_before_ms=pause_before, pause_after_ms=0
            )
        )
        if open_line:
            open_line.trail = _sentences(between)[0]
        lead = _sentences(between)[-1]
        open_line = SpokenLine(
            text=dialogue_text, lead=lead, beat_emotion=beat_emotion, lead_in=lead if between else paragraph_tail
        )
        lines.append(open_line)
        between = []
        paragraph_tail = ""

    if open_line:
        open_line.trail = _sentences(between)[0]
    if not chunks:
        return [], []

    # First chunk should not have a leading pause
    chunks[0].pause_before_ms = 0
    return chunks, lines
