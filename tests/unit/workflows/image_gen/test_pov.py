"""Camera resolution: the four levers, and the grid math behind the classifier.

No model and no network — `local_ml.aclassify_pov` is monkeypatched everywhere, and
the grid math is exercised through the pure `pov_from_logits`.
"""

from __future__ import annotations

import pytest

from backend.inference import local_ml
from backend.workflows.image_gen import pov


def _history(*roles_and_texts: tuple[str, str]) -> list[dict]:
    return [{"role": role, "content": text} for role, text in roles_and_texts]


def _fake_classifier(monkeypatch, labels: list[str]) -> list[str]:
    """Answer with `labels` in order; record every text the classifier was shown."""
    seen: list[str] = []
    queue = list(labels)

    async def fake(text: str) -> str:
        seen.append(text)
        return queue.pop(0) if queue else "ambiguous"

    monkeypatch.setattr(local_ml, "aclassify_pov", fake)
    monkeypatch.setattr(pov, "classifier_ready", _always_ready)
    return seen


async def _always_ready() -> bool:
    return True


async def _never_ready() -> bool:
    return False


# --- the grid ------------------------------------------------------------------


@pytest.mark.parametrize("row,label", list(enumerate(local_ml.POV_ROWS)))
@pytest.mark.parametrize("tense", range(3))
def test_pov_from_logits_reads_the_grid_row_major(row, label, tense):
    # Layout is POV rows x tense columns; index = row * 3 + column. Transposing it
    # would silently map "first" onto "past" and still return a plausible label.
    grid = [0.0] * 12
    grid[row * 3 + tense] = 9.0
    assert local_ml.pov_from_logits(grid) == label


def test_pov_from_logits_marginalizes_tense_rather_than_taking_the_top_cell():
    grid = [0.0] * 12
    grid[0] = 3.0  # "first", concentrated in one tense
    grid[6] = grid[7] = grid[8] = 2.0  # "third", spread across all three
    assert local_ml.pov_from_logits(grid) == "third"


# --- the levers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "appearance, expected",
    [
        ("3D, third_person", pov.THIRD),
        ("3D, third-person", pov.THIRD),
        ("3D, Third Person", pov.THIRD),
        ("3D, 3rd person", pov.THIRD),
        ("anime, first_person", pov.FIRST),
        ("anime, 1st-person", pov.FIRST),
        ("anime, pov", pov.FIRST),
        ("blonde hair, 3D, personal trainer", None),  # 'person' alone is not a camera tag
        ("", None),
        (None, None),
    ],
)
def test_pinned_viewpoint_reads_a_camera_tag_in_any_spelling(appearance, expected):
    assert pov.pinned_viewpoint(appearance) == expected


@pytest.mark.parametrize(
    "value, expected",
    [("auto", "auto"), ("first", "first"), ("third", "third"), ("nonsense", "auto"), (None, "auto"), (7, "auto")],
)
def test_normalize_mode_falls_back_to_the_default(value, expected):
    assert pov.normalize_mode(value) == expected


async def test_character_tag_beats_manual_and_the_classifier(monkeypatch):
    seen = _fake_classifier(monkeypatch, ["first"])
    result = await pov.resolve(
        appearance="video game asset, 3D, third_person",
        mode="first",
        history=_history(("assistant", "You feel her hand on yours.")),
    )
    assert result == (pov.THIRD, "character_tag")
    assert seen == []  # a pinned camera short-circuits before any model runs


@pytest.mark.parametrize("mode, expected", [("first", pov.FIRST), ("third", pov.THIRD)])
async def test_manual_mode_beats_the_classifier(monkeypatch, mode, expected):
    seen = _fake_classifier(monkeypatch, ["third" if mode == "first" else "first"])
    assert await pov.resolve(mode=mode, history=_history(("assistant", "She steps closer."))) == (expected, "manual")
    assert seen == []


@pytest.mark.parametrize("label, expected", [("first", pov.FIRST), ("second", pov.FIRST), ("third", pov.THIRD)])
async def test_first_and_second_person_share_the_camera(monkeypatch, label, expected):
    _fake_classifier(monkeypatch, [label])
    assert await pov.resolve(mode="auto", history=_history(("assistant", "x"))) == (expected, "classifier")


async def test_ambiguous_walks_back_to_the_previous_decided_message(monkeypatch):
    seen = _fake_classifier(monkeypatch, ["ambiguous", "ambiguous", "third"])
    result = await pov.resolve(
        mode="auto",
        history=_history(
            ("assistant", "oldest"),
            ("user", "ignored"),
            ("assistant", "middle"),
            ("assistant", "anchor"),
        ),
    )
    assert result == (pov.THIRD, "classifier")
    # Newest first, assistant turns only -- the user's persona voice need not match
    # the camera of the reply being illustrated.
    assert seen == ["anchor", "middle", "oldest"]


async def test_lookback_is_bounded_then_falls_to_the_default(monkeypatch):
    seen = _fake_classifier(monkeypatch, ["ambiguous"] * 10)
    history = _history(*[("assistant", f"m{i}") for i in range(8)])
    assert await pov.resolve(mode="auto", history=history) == (pov.DEFAULT_POV, "default")
    assert len(seen) == pov.LOOKBACK


async def test_auto_without_a_classifier_degrades_to_the_default(monkeypatch):
    monkeypatch.setattr(pov, "classifier_ready", _never_ready)

    async def explode(_text):
        raise AssertionError("classifier must not run when it is not ready")

    monkeypatch.setattr(local_ml, "aclassify_pov", explode)
    assert await pov.resolve(mode="auto", history=_history(("assistant", "x"))) == (pov.DEFAULT_POV, "default")


async def test_a_failing_classifier_degrades_instead_of_raising(monkeypatch):
    async def boom(_text):
        raise RuntimeError("failed to load: wrong head?")

    monkeypatch.setattr(local_ml, "aclassify_pov", boom)
    monkeypatch.setattr(pov, "classifier_ready", _always_ready)
    # A local-ML fault must cost the camera, not the image.
    assert await pov.resolve(mode="auto", history=_history(("assistant", "x"))) == (pov.DEFAULT_POV, "default")


async def test_empty_history_skips_the_classifier(monkeypatch):
    seen = _fake_classifier(monkeypatch, ["first"])
    assert await pov.resolve(mode="auto", history=()) == (pov.DEFAULT_POV, "default")
    assert seen == []
