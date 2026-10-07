"""Subject fixation: the draft describes a subject that most recent replies already described."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

DESCRIPTION = 2  # index of "description" in each category's (absent, action, description) probabilities

#: ``{category: [absent, action, description]}``.
SubjectProbs = Mapping[str, Sequence[float]]


@dataclass(frozen=True)
class StreakRule:
    window: int  # how many previous replies are read
    min_count: int  # how many of them must describe the subject
    min_prob: float  # P(description) at which one reply counts as describing it


# Measured with Orb's GGUF tagger on app.db's solo active branches (6,999 drafts): fires on 20% of drafts, in 271 of 482 chats.
# face and voice are present in most replies, so they need every reply in the window; skin and voice are the noisiest heads,
# so a reply counts only at high confidence.
DEFAULT_RULE = StreakRule(window=4, min_count=3, min_prob=0.6)
RULES: dict[str, StreakRule] = {
    "face": StreakRule(window=4, min_count=4, min_prob=0.7),
    "skin": StreakRule(window=4, min_count=3, min_prob=0.8),
    "voice": StreakRule(window=4, min_count=4, min_prob=0.8),
}
HISTORY_WINDOW = max(rule.window for rule in (DEFAULT_RULE, *RULES.values()))

LABELS: dict[str, str] = {
    "breath": "breathing or heartbeat",
    "build": "build or figure",
    "lower_body": "hips and legs",
    "neck": "neck and shoulders",
    "accessory": "jewelry and accessories",
    "object": "held items",
    "nonhuman": "non-human features (halo, horns, wings, tail)",
    "light": "the scene's light and shadow",
    "sound": "background sounds",
    "weather": "weather and air",
}


@dataclass(frozen=True)
class SubjectStreak:
    category: str
    count: int  # previous replies that described it
    window: int  # previous replies read

    @property
    def reason(self) -> str:
        label = LABELS.get(self.category, self.category)
        return f"The draft describes {label} again (already described in {self.count} of the last {self.window} replies)."


def detect_subject_fixation(draft: SubjectProbs, history: Sequence[SubjectProbs]) -> list[SubjectStreak]:
    """Streaks the draft extends. *history* is newest first; a rule needs its whole window, so short chats never fire."""
    streaks: list[SubjectStreak] = []
    for category, probs in draft.items():
        rule = RULES.get(category, DEFAULT_RULE)
        if probs[DESCRIPTION] < rule.min_prob or len(history) < rule.window:
            continue
        count = sum(1 for reply in history[: rule.window] if reply.get(category, (1.0, 0.0, 0.0))[DESCRIPTION] >= rule.min_prob)
        if count >= rule.min_count:
            streaks.append(SubjectStreak(category, count, rule.window))
    return streaks
