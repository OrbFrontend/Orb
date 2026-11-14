"""Map a line's emotion label to ZONOS steering controls."""

from __future__ import annotations

import math
from collections.abc import Mapping

_ANGRY = "emotions.angry"
_SURPRISED = "emotions.surprised"
_SAD = "emotions.sad"
_DISTRESSED = "emotions.distressed"
_SERENE = "emotions.serene"
_WHISPER = "delivery.whisper"

# Every go-emotions label, plus the whispered beat, at the doses that sounded right on reference-cloned voices. None speaks
# the line with VOICE_CONTROLS.
DELIVERY_TABLE: Mapping[str, tuple[str, float] | None] = {
    "admiration": None,
    "amusement": None,
    "anger": (_ANGRY, 0.25),
    "annoyance": (_ANGRY, 0.25),
    "approval": None,
    "caring": (_SERENE, 0.75),
    "confusion": None,
    "curiosity": None,
    "desire": None,
    "disappointment": (_SAD, 1.5),
    "disapproval": (_ANGRY, 0.25),
    "disgust": None,
    "embarrassment": None,
    "excitement": None,
    "fear": (_DISTRESSED, 1.0),
    "gratitude": (_SERENE, 0.75),
    "grief": (_SAD, 1.5),
    "joy": None,
    "love": (_SERENE, 0.75),
    "nervousness": (_DISTRESSED, 1.0),
    "optimism": None,
    "pride": None,
    "realization": (_SURPRISED, 0.375),
    "relief": (_SERENE, 0.75),
    "remorse": (_SAD, 1.5),
    "sadness": (_SAD, 1.5),
    "surprise": (_SURPRISED, 0.375),
    "neutral": None,
    "whispered": (_WHISPER, 1.0),
}

# No strength setting can push a direction past these.
DOSE_CAPS: Mapping[str, float] = {
    _ANGRY: 1.5,
    _SURPRISED: 1.75,
    _SAD: 4.0,
    _DISTRESSED: 2.0,
    _SERENE: 1.5,
    _WHISPER: 2.0,
}

MAX_STRENGTH = 2.0

#: The steering a line without an emotion control gets: it brought reference-cloned voices closest to their clip by ear.
VOICE_CONTROLS: Mapping[str, float] = {_SAD: 1.5}


def delivery_controls(label: str, strength: float = 1.0) -> dict[str, float]:
    """The ``controls`` body for *label* at *strength* times the table dose; empty for no control."""
    row = DELIVERY_TABLE.get(label)
    if row is None or not math.isfinite(strength) or strength <= 0:
        return {}
    control, dose = row
    return {control: round(min(dose * min(strength, MAX_STRENGTH), DOSE_CAPS[control]), 3)}
