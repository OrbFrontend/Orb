"""Pick one go-emotions label per spoken line for emotion-driven delivery."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

from ..toolkit import Settings, classify_emotions, local_feature_ready
from .engine.regex_extractor import SpokenLine

logger = logging.getLogger(__name__)

EMOTION_FEATURE = "emotion_classifier"
LABEL_THRESHOLD = 0.5
WHISPERED = "whispered"
_SENTENCE_ENDS = tuple(".!?…。！？؟")
WHISPER_REACH = 6  # narration words on each side of a line that can whisper it, enough for "she said in a low whisper"


def pick_label(line_scores: Mapping[str, float], window_scores: Mapping[str, float]) -> str:
    """The top non-neutral label of the averaged scores when it reaches the threshold and beats ``neutral``."""
    averaged = {label: (score + window_scores.get(label, 0.0)) / 2 for label, score in line_scores.items()}
    neutral = averaged.pop("neutral", 0.0)
    if not averaged:
        return "neutral"
    label, score = max(averaged.items(), key=lambda item: item[1])
    return label if score >= LABEL_THRESHOLD and score > neutral else "neutral"


def _whisper_word(word: str) -> bool:
    return "".join(ch for ch in word if ch.isalpha()).lower().startswith("whisper")


def _trail_attaches(line: SpokenLine) -> bool:
    """The trail continues the line's sentence (``"Run," she whispered``), so it is not the next line's lead-in."""
    first = line.trail[:1]
    return bool(first) and (first.islower() or not line.text.rstrip(" '\"”’»›」』").endswith(_SENTENCE_ENDS))


def whispered(line: SpokenLine) -> bool:
    """A whispered beat before *line*, or a whisper word within ``WHISPER_REACH`` narration words of it."""
    near = line.lead_in.split()[-WHISPER_REACH:]
    if _trail_attaches(line):
        near += line.trail.split()[:WHISPER_REACH]
    return line.beat_emotion == WHISPERED or any(_whisper_word(word) for word in near)


def _english(language: str) -> bool:
    return (language or "en").strip().lower().replace("_", "-").split("-")[0] == "en"


async def line_labels(lines: Sequence[SpokenLine], profile: Mapping, settings: Settings | None) -> list[str]:
    """One label per line: a nearby whisper wins, else the classifier's label, else ``neutral``."""
    labels = [WHISPERED if whispered(line) else "neutral" for line in lines]
    open_lines = [i for i, label in enumerate(labels) if label != WHISPERED]
    if (
        not open_lines
        or not profile.get("emotion_delivery")
        or not _english(str(profile.get("language") or ""))
        or settings is None
        or not local_feature_ready(EMOTION_FEATURE, settings)
    ):
        return labels
    texts = [text for i in open_lines for text in (lines[i].text, lines[i].window)]
    try:
        scores = await classify_emotions(texts)
    except Exception as exc:  # a classifier fault speaks the lines plainly rather than failing the clip
        logger.warning("emotion classifier failed (%r); speaking without delivery controls", exc)
        return labels
    for n, i in enumerate(open_lines):
        labels[i] = pick_label(scores[2 * n], scores[2 * n + 1])
    return labels
