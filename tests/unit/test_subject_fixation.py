"""The subject-fixation streak counter and its enable gate."""

from typing import cast

import pytest

from backend.analysis.detectors.subject_fixation import PRESENT, confirm, nominate, presence_streaks
from backend.core.settings import Settings
from backend.inference import local_ml
from backend.pipeline import subject_tags

DESCRIBED, ACTED, ABSENT = (0.05, 0.05, 0.9), (0.05, 0.9, 0.05), (1.0, 0.0, 0.0)


def _replies(category: str, pattern: str) -> list[dict]:
    """Newest-first history from a pattern string: d = described, a = action only, . = absent."""
    level = {"d": DESCRIBED, "a": ACTED, ".": ABSENT}
    return [{category: level[ch]} for ch in pattern]


@pytest.mark.parametrize(
    "history,nominated",
    [
        ("d......d", True),  # 2 of the last 8
        ("d.......", False),
        ("aaaaaaaa", False),  # "her eyes roll back" every reply is action, not description
        ("dd", True),  # a chat repeats from its first replies, so two are enough
        ("........dddd", False),  # only the last 8 replies count
    ],
)
def test_the_tagger_nominates_a_subject_described_in_recent_replies(history, nominated):
    assert (nominate({"eyes": DESCRIBED}, _replies("eyes", history)) == ["eyes"]) is nominated


def test_a_draft_that_does_not_describe_the_subject_is_never_nominated():
    assert nominate({"eyes": ACTED}, _replies("eyes", "dddddddd")) == []


def test_a_repeat_is_confirmed_from_two_of_the_last_eight_replies():
    streak = confirm("eyes", [0.9, None, 0.2, 0.1, 0.1, 0.1, 0.1, 0.65])
    assert streak is not None and (streak.count, streak.window) == (2, 8)
    assert confirm("eyes", [0.9, 0.55, None, None, None, None, None, None]) is None  # an unread reply never counts
    assert confirm("eyes", [0.9] + [0.1] * 7 + [0.9]) is None  # only the last 8 count
    short = confirm("eyes", [0.9, 0.7])  # the third reply of a chat
    assert short is not None and (short.count, short.window) == (2, 2)


@pytest.mark.parametrize(
    "history,fires",
    [
        ("adad", True),  # the eyes in every reply, worded anew each time
        ("ada.", False),
        ("aaa", False),  # a chat needs four earlier replies
        ("dddd....", True),  # only the last four count
    ],
)
def test_any_subject_in_every_recent_reply_fires_as_a_presence_streak(history, fires):
    history_tags = [{**reply, "voice": DESCRIBED} for reply in _replies("eyes", history)]
    streaks = presence_streaks({"eyes": ACTED, "voice": ABSENT}, history_tags)  # a subject the draft leaves out never fires
    assert [(s.category, s.count, s.level) for s in streaks] == ([("eyes", 4, PRESENT)] if fires else [])


def _settings(toggle: bool | None, local: dict | None = None, *, agent: bool = True, auditor: bool = True) -> Settings:
    toggles = {} if toggle is None else {"subject_fixation": toggle}
    return cast(
        Settings,
        {
            "enable_agent": int(agent),
            "enabled_tools": {"editor_apply_patch": auditor},
            "editor_audit_toggles": toggles,
            "local_ml_enabled": local or {},
        },
    )


@pytest.mark.parametrize(
    "toggle,local,missing,agent,auditor,enabled",
    [
        (True, {}, None, True, True, True),
        (None, {}, None, True, True, False),  # off by default
        (False, {}, None, True, True, False),
        (True, {"subjects_classifier": False}, None, True, True, False),  # disabled in Local ML
        (True, {}, "subjects_classifier", True, True, False),  # model absent
        (True, {}, None, False, True, False),  # the Output Auditor needs the Agent
        (True, {}, None, True, False, False),  # and its own toggle
    ],
)
def test_subjects_enabled_needs_the_auditor_the_toggle_and_the_ready_analyzer(
    monkeypatch, toggle, local, missing, agent, auditor, enabled
):
    monkeypatch.setattr(local_ml, "available", lambda feature: (feature != missing, ""))
    assert subject_tags.subjects_enabled(_settings(toggle, local, agent=agent, auditor=auditor)) is enabled
