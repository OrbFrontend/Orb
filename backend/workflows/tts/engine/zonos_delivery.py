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

# Every go-emotions label, plus the whispered beat. None speaks the line without a control.
DELIVERY_TABLE: Mapping[str, tuple[str, float] | None] = {
    "admiration": None,
    "amusement": None,
    "anger": (_ANGRY, 0.5),
    "annoyance": (_ANGRY, 0.5),
    "approval": None,
    "caring": (_SERENE, 1.5),
    "confusion": None,
    "curiosity": None,
    "desire": None,
    "disappointment": (_SAD, 3.0),
    "disapproval": (_ANGRY, 0.5),
    "disgust": None,
    "embarrassment": None,
    "excitement": None,
    "fear": (_DISTRESSED, 2.0),
    "gratitude": (_SERENE, 1.5),
    "grief": (_SAD, 3.0),
    "joy": None,
    "love": (_SERENE, 1.5),
    "nervousness": (_DISTRESSED, 2.0),
    "optimism": None,
    "pride": None,
    "realization": (_SURPRISED, 0.75),
    "relief": (_SERENE, 1.5),
    "remorse": (_SAD, 3.0),
    "sadness": (_SAD, 3.0),
    "surprise": (_SURPRISED, 0.75),
    "neutral": None,
    "whispered": (_WHISPER, 2.0),
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


def delivery_controls(label: str, strength: float = 1.0) -> dict[str, float]:
    """The ``controls`` body for *label* at *strength* times the table dose; empty for no control."""
    row = DELIVERY_TABLE.get(label)
    if row is None or not math.isfinite(strength) or strength <= 0:
        return {}
    control, dose = row
    return {control: round(min(dose * min(strength, MAX_STRENGTH), DOSE_CAPS[control]), 3)}
