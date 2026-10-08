"""Subject fixation: the draft re-describes a subject recent replies described, or brings in one every recent reply had."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from ..subjects import SUBJECT_DESCRIPTIONS

ABSENT, DESCRIPTION = 0, 2  # indices in each category's (absent, action, description) probabilities
PRESENT = -1  # a level read as 1 - absent: acted or described

#: ``{category: [absent, action, description]}``.
SubjectProbs = Mapping[str, Sequence[float]]


@dataclass(frozen=True)
class StreakRule:
    window: int  # how many previous replies are read
    min_count: int  # how many of them must reach min_prob
    min_prob: float  # the probability at which one reply counts
    level: int = DESCRIPTION


# The tagger nominates subjects the draft and recent replies describe; it cannot tell a repeated description from a new one.
NOMINATE_RULE = StreakRule(window=8, min_count=2, min_prob=0.5)
# The Judge confirms a nominee: its repeat probability against each earlier reply, read pairwise.
REPEAT_RULE = StreakRule(window=8, min_count=2, min_prob=0.6)
# A subject in every recent reply is recurring focus however it is worded; flag it on presence alone.
PRESENCE_RULE = StreakRule(window=4, min_count=4, min_prob=0.7, level=PRESENT)
HISTORY_WINDOW = max(rule.window for rule in (NOMINATE_RULE, REPEAT_RULE, PRESENCE_RULE))


@dataclass(frozen=True)
class SubjectStreak:
    category: str
    count: int  # previous replies the draft repeats, or that had the subject at all
    window: int  # previous replies read
    level: int = DESCRIPTION

    @property
    def reason(self) -> str:
        label = SUBJECT_DESCRIPTIONS.get(self.category, self.category)
        if self.level == PRESENT:
            return f"{label}: mentioned in the draft and all {self.window} recent replies."
        return f"{label}: the draft repeats a descriptive detail from {self.count} of the last {self.window} replies."


def _reaches(probs: Sequence[float] | None, rule: StreakRule) -> bool:
    if probs is None:
        return False
    return (1.0 - probs[ABSENT] if rule.level == PRESENT else probs[rule.level]) >= rule.min_prob


def _count(history: Sequence[SubjectProbs], category: str, rule: StreakRule) -> int:
    """Replies in *rule*'s window that reach it; a chat shorter than the window counts what it has."""
    return sum(1 for reply in history[: rule.window] if _reaches(reply.get(category), rule))


def nominate(draft: SubjectProbs, history: Sequence[SubjectProbs]) -> list[str]:
    """Categories the draft describes and recent replies (newest first) described, for the Judge to read."""
    rule = NOMINATE_RULE
    return [
        category
        for category, probs in draft.items()
        if _reaches(probs, rule) and _count(history, category, rule) >= rule.min_count
    ]


def confirm(category: str, repeat_probs: Sequence[float | None]) -> SubjectStreak | None:
    """The streak when the draft repeats *category*'s description from enough earlier replies (newest first; None = unread)."""
    rule = REPEAT_RULE
    read = repeat_probs[: rule.window]
    count = sum(1 for p in read if p is not None and p >= rule.min_prob)
    return SubjectStreak(category, count, len(read)) if count >= rule.min_count else None


def presence_streaks(draft: SubjectProbs, history: Sequence[SubjectProbs]) -> list[SubjectStreak]:
    """Subjects the draft has that every recent reply (newest first) also had, read from tags alone."""
    rule = PRESENCE_RULE
    return [
        SubjectStreak(category, count, rule.window, rule.level)
        for category, probs in draft.items()
        if _reaches(probs, rule) and (count := _count(history, category, rule)) >= rule.min_count
    ]
