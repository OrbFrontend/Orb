"""The subject-fixation streak counter and its enable gate."""

from typing import cast

import pytest

from backend.analysis.detectors.subject_fixation import DEFAULT_RULE, RULES, detect_subject_fixation
from backend.core.settings import Settings
from backend.inference import local_ml
from backend.pipeline import subject_tags

DESCRIBED, ACTED, ABSENT = (0.05, 0.05, 0.9), (0.05, 0.9, 0.05), (1.0, 0.0, 0.0)


def _replies(category: str, pattern: str) -> list[dict]:
    """Newest-first history from a pattern string: d = described, a = action only, . = absent."""
    level = {"d": DESCRIBED, "a": ACTED, ".": ABSENT}
    return [{category: level[ch]} for ch in pattern]


@pytest.mark.parametrize(
    "history,fires",
    [
        ("ddd.", True),  # 3 of the last 4
        ("dd..", False),  # 2 of 4
        ("ddda", True),
        ("aaaa", False),  # "her eyes roll back" every reply is action, not a streak
        ("ddd", False),  # a short chat has no whole window
        ("....dddd", False),  # only the last 4 replies count
    ],
)
def test_eyes_streak_counts_description_only(history, fires):
    assert bool(detect_subject_fixation({"eyes": DESCRIBED}, _replies("eyes", history))) is fires


def test_a_draft_that_does_not_describe_the_subject_never_fires():
    assert not detect_subject_fixation({"eyes": ACTED}, _replies("eyes", "dddd"))


def test_face_needs_every_reply_in_the_window():
    assert not detect_subject_fixation({"face": DESCRIBED}, _replies("face", "ddd."))
    assert detect_subject_fixation({"face": DESCRIBED}, _replies("face", "dddd"))


@pytest.mark.parametrize("category", ["skin", "voice"])
def test_low_confidence_heads_count_only_confident_descriptions(category):
    unsure = (0.15, 0.15, 0.7)  # a description at 0.7 counts for eyes, not here
    history = [{category: unsure}] * 4
    assert not detect_subject_fixation({category: unsure}, history)
    assert detect_subject_fixation({"eyes": unsure}, [{"eyes": unsure}] * 4)
    assert RULES[category].min_prob > DEFAULT_RULE.min_prob


def test_the_reason_names_the_count():
    [streak] = detect_subject_fixation({"lower_body": DESCRIBED}, _replies("lower_body", "d.dd"))
    assert (streak.category, streak.count, streak.window) == ("lower_body", 3, 4)


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
    "toggle,local,model,agent,auditor,enabled",
    [
        (True, {}, True, True, True, True),
        (None, {}, True, True, True, False),  # off by default
        (False, {}, True, True, True, False),
        (True, {"subjects_classifier": False}, True, True, True, False),  # disabled in Local ML
        (True, {}, False, True, True, False),  # model absent
        (True, {}, True, False, True, False),  # the Output Auditor needs the Agent
        (True, {}, True, True, False, False),  # and its own toggle
    ],
)
def test_subjects_enabled_needs_the_auditor_the_toggle_and_a_ready_model(
    monkeypatch, toggle, local, model, agent, auditor, enabled
):
    monkeypatch.setattr(local_ml, "available", lambda feature: (model, ""))
    assert subject_tags.subjects_enabled(_settings(toggle, local, agent=agent, auditor=auditor)) is enabled
