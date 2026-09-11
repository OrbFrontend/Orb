"""Camera resolution: the three levers, and the grid math behind the classifier.

No model and no network — `local_ml.aclassify_pov` is monkeypatched everywhere, and
the grid math is exercised through the pure `pov_from_logits`.
"""

from __future__ import annotations

import pytest

from backend.analysis.format_consistency import Dialogue, classify_axes, narration_only
from backend.inference import local_ml
from backend.inference.local_models import assets, dependencies
from backend.workflows.image_gen import pov


@pytest.fixture(autouse=True)
def _no_local_models(monkeypatch):
    """Markup is read by `classify_axes`, whatever sits in the local models directory."""
    monkeypatch.setattr(assets, "present", lambda feature: False)


def _history(*roles_and_texts: tuple[str, str]) -> list[dict]:
    return [{"role": role, "content": text} for role, text in roles_and_texts]


def _ready(monkeypatch, ready: bool = True) -> None:
    """Set the camera's gate (POV model installed and enabled) without a database."""

    async def no_settings() -> dict:
        return {}

    monkeypatch.setattr(pov, "get_settings", no_settings)
    monkeypatch.setattr(pov, "local_feature_ready", lambda feature, settings: ready)


def _fake_classifier(monkeypatch, labels: list[str]) -> list[str]:
    """Answer with `labels` in order; record every text the classifier was shown."""
    seen: list[str] = []
    queue = list(labels)

    async def fake(text: str) -> str:
        seen.append(text)
        return queue.pop(0) if queue else "ambiguous"

    monkeypatch.setattr(local_ml, "aclassify_pov", fake)
    _ready(monkeypatch)
    return seen


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


# `normalize_mode` itself is pinned through the config route it exists for, in
# `test_hooks::test_pov_mode_is_global_config`.


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
    _ready(monkeypatch, False)

    async def explode(_text):
        raise AssertionError("classifier must not run when it is not ready")

    monkeypatch.setattr(local_ml, "aclassify_pov", explode)
    # Its own source: "the classifier is not there" and "the classifier could not
    # decide" are fixed in different places, so the attachment must tell them apart.
    assert await pov.resolve(mode="auto", history=_history(("assistant", "x"))) == (pov.DEFAULT_POV, "no_classifier")
    # The mode the picker names in that label resolves to the same camera.
    assert await pov.resolve(mode=pov.DEFAULT_POV_MODE, history=()) == (pov.DEFAULT_POV, "manual")


async def test_a_failing_classifier_degrades_instead_of_raising(monkeypatch):
    async def boom(_text):
        raise RuntimeError("failed to load: wrong head?")

    monkeypatch.setattr(local_ml, "aclassify_pov", boom)
    _ready(monkeypatch)
    # A local-ML fault must cost the camera, not the image.
    assert await pov.resolve(mode="auto", history=_history(("assistant", "x"))) == (pov.DEFAULT_POV, "default")


async def test_empty_history_skips_the_classifier(monkeypatch):
    seen = _fake_classifier(monkeypatch, ["first"])
    assert await pov.resolve(mode="auto", history=()) == (pov.DEFAULT_POV, "default")
    assert seen == []


@pytest.mark.parametrize("marker", ["*", "_"])
async def test_bare_speech_cannot_displace_the_camera_narration(monkeypatch, marker):
    text = f"{marker}Mara placed the lantern on the table.{marker} " + "I need you here. You promised. I trusted you. " * 8
    seen = _fake_classifier(monkeypatch, ["third"])
    assert await pov.resolve(history=_history(("assistant", text))) == (pov.THIRD, "classifier")
    assert seen == ["Mara placed the lantern on the table."]


async def test_camera_parses_each_history_rows_own_dialogue_convention(monkeypatch):
    seen = _fake_classifier(monkeypatch, ["ambiguous", "first"])
    history = _history(("assistant", "*I raised the lantern.* Come closer."), ("assistant", '"I am still here."'))
    assert await pov.resolve(history=history) == (pov.FIRST, "classifier")
    assert seen == ["", "I raised the lantern."]


async def test_the_markup_classifier_decides_what_the_camera_reads(monkeypatch):
    """Installed, the markup model's dialogue reading chooses which spans are narration."""
    text = "*I raised the lantern.* Come closer."
    assert classify_axes(text).dialogue == Dialogue.BARE
    seen = _fake_classifier(monkeypatch, ["first"])

    async def reads_quoted(_text: str) -> tuple[str, str]:
        return "asterisk", "quoted"

    monkeypatch.setattr(assets, "present", lambda feature: True)
    monkeypatch.setattr(dependencies, "deps_ok", lambda feature=None: (True, ""))
    monkeypatch.setattr(local_ml, "aclassify_markup", reads_quoted)

    assert await pov.resolve(history=_history(("assistant", text))) == (pov.FIRST, "classifier")
    assert seen == [narration_only(text, Dialogue.QUOTED)]
    assert seen != [narration_only(text, Dialogue.BARE)]
