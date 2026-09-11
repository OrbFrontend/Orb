"""Pure local-ML scaffold helpers: resolve_path / present / deps_ok. No model,
no network — the route-level tri-state lives in tests/integration/test_local_ml.py.

PATCHES GO ON THE MODULE THAT OWNS THE NAME. ``local_ml`` re-exports the asset
and dependency surface for callers that address it by that name, but those are
second bindings: production reads ``local_models``' own copies, so patching a
re-export would leave the code under test running against the real disk.
"""

from __future__ import annotations

import os

import pytest

from backend.inference import local_ml
from backend.inference.local_models import assets, dependencies


def test_resolve_path_env_override_wins(monkeypatch, tmp_path):
    custom = tmp_path / "custom.gguf"
    custom.write_bytes(b"")  # override only wins if the file exists (no stale hiding a real model)
    monkeypatch.setenv("ORB_AUTOCOMPLETE_MODEL", str(custom))
    assert assets.resolve_path("autocomplete") == str(custom)


def test_present_reflects_disk(monkeypatch):
    monkeypatch.setattr(assets, "resolve_path", lambda f: "/nope/missing.gguf")
    assert assets.present("autocomplete") is False


def test_deps_ok_reports_missing_extra(monkeypatch):
    # The missing-extra branch is the one with a contract: the reason has to name
    # the requirements file the user is meant to install. Forced rather than
    # inferred from the environment -- when the extras happen to be present this
    # asserted nothing at all, and paid a real llama_cpp import (~1s) to do it.
    def _boom():
        raise ModuleNotFoundError("No module named 'llama_cpp'")

    monkeypatch.setattr(dependencies, "import_llama", _boom)
    ok, reason = dependencies.deps_ok()
    assert ok is False
    assert "requirements-ml.txt" in reason


def test_install_cmd_paths_are_absolute():
    # Pasted into a fresh cmd prompt with no cwd in the repo, so neither the
    # interpreter nor the requirements file may be relative.
    cmd = dependencies.install_cmd()
    req = cmd.split("-r ", 1)[1].strip('"')
    assert os.path.isabs(req) and req.endswith("requirements-ml.txt")
    assert os.path.isabs(cmd.split(" -m ", 1)[0].strip('"'))


def test_model_dir_is_created(monkeypatch, tmp_path):
    monkeypatch.setattr(assets, "_ROOT", str(tmp_path))
    d = assets.model_dir()
    assert os.path.isdir(d)
    assert d.endswith(os.path.join("backend", "data", "models"))


def test_pov_input_treats_every_line_break_as_a_hard_sentence_edge():
    text = "discarded first\ndiscarded second\r\nkept third\u2028kept fourth\nkept fifth"
    shaped = local_ml.pov_input(text)
    assert shaped == "kept third kept fourth kept fifth"
    assert not any(mark in shaped for mark in "\r\n\u2028")


def test_pov_input_uses_core_quote_and_sentence_policy():
    text = "Old。 ‘I don’t count.’ Second؟ 「Nor do I。」 Third. Fourth."
    assert local_ml.pov_input(text) == "Second؟ Third. Fourth."


# --- the tense half of the povtense grid ----------------------------------------
# The POV half is pinned by its first consumer, in
# tests/unit/workflows/image_gen/test_pov.py; the tense half has no single owning
# workflow, so its grid math is pinned here beside the model surface itself.


@pytest.mark.parametrize("col,label", list(enumerate(local_ml.TENSE_COLS)))
@pytest.mark.parametrize("row", range(4))
def test_tense_from_logits_reads_the_grid_column_major(col, label, row):
    # Layout is POV rows x tense columns; index = row * 3 + column. Reading it
    # transposed would map "past" onto "first" and still return a plausible label,
    # which is exactly the failure no downstream assertion would catch.
    grid = [0.0] * 12
    grid[row * 3 + col] = 9.0
    assert local_ml.tense_from_logits(grid) == label


def test_tense_from_logits_marginalizes_pov_rather_than_taking_the_top_cell():
    grid = [0.0] * 12
    grid[1] = 3.0  # "present", concentrated in one POV row
    grid[0] = grid[3] = grid[6] = 2.0  # "past", spread across three rows
    assert local_ml.tense_from_logits(grid) == "past"


def test_the_two_margins_read_the_same_grid_independently():
    # One cell lit: the pair must name that cell's row AND its column. This is the
    # invariant `aclassify_pov_tense` sells -- both labels off one forward pass.
    grid = [0.0] * 12
    grid[1 * 3 + 0] = 9.0  # row "second", column "past"
    assert (local_ml.pov_from_logits(grid), local_ml.tense_from_logits(grid)) == ("second", "past")


async def test_aclassify_pov_tense_short_circuits_on_empty_shaping(monkeypatch):
    """An all-dialogue reply shapes to "" -- the model is never loaded for it."""

    def boom(*args, **kwargs):
        raise AssertionError("the model must not be reached for empty shaped input")

    monkeypatch.setattr(local_ml, "_head_logits", boom)
    assert local_ml.pov_input('"Just talking." "Only dialogue here."') == ""
    assert await local_ml.aclassify_pov_tense('"Just talking." "Only dialogue here."') == ("ambiguous", "ambiguous")
    # The single-label door is the same blocking core, so it short-circuits too.
    assert await local_ml.aclassify_pov("") == "ambiguous"


# --- markup classifier input shaping ------------------------------------------
# Shared with ../RP-Markup-Classifier: its build manifests record
# MARKUP_INPUT_VERSION and a digest of these outputs, so a shaping change that
# does not bump the version fails that repo's audit instead of silently mixing
# two input distributions in one training build.


@pytest.mark.parametrize(
    "text,expected",
    [
        ("*She nods.* Fine.", "*She nods.* Fine."),  # RP markup is the signal: kept
        ("“Hi,” she said. «Oui» 「はい」 ❝x❞",) * 2,  # no glyph normalization
        ("She **really** meant it.", "She   meant it."),  # one space per protected run
        ("Before\n```\n*code* here\n```\nAfter", "Before\n \nAfter"),  # fences span lines
        ("Unclosed ```fence *keeps* going", "Unclosed ```fence *keeps* going"),  # incomplete runs stay
        ("**bold that never closes\n*real beat*", "**bold that never closes\n*real beat*"),
        ("Scene one.\n\n***\n\nScene two.", "Scene one.\n\n \n\nScene two."),
        ("Here is a list:\n* rope\n* a lantern", "Here is a list:\n- rope\n- a lantern"),  # bullets are not beats
        ("  * indented\r\n\t* after a CRLF", "  - indented\r\n\t- after a CRLF"),
        ("* She waves. *", "* She waves. *"),  # closes later on its line: a sloppy beat, not a list
        ("He said *so* * and left", "He said *so* * and left"),  # mid-line stars are never bullets
        ("", ""),
    ],
)
def test_markup_input_golden_strings(text, expected):
    assert local_ml.markup_input(text) == expected


def test_markup_input_caps_a_runaway_message():
    assert local_ml.markup_input("x" * (local_ml.MARKUP_INPUT_CHARS + 50)) == "x" * local_ml.MARKUP_INPUT_CHARS


def test_markup_input_hides_exactly_what_classify_axes_hides():
    from backend.analysis import format_consistency

    for text in ("A **b** c", "x\n```\ny\n```\nz", "one ___ two", "*kept* __bold__"):
        assert local_ml.markup_input(text) == format_consistency._strip_protected(text)


# --- the markup head ------------------------------------------------------------
# Narration rows x dialogue columns, row-major (../RP-Markup-Classifier/src/schema.py).
# A transposed read maps "asterisk" onto "quoted" and still looks plausible, so
# every cell is pinned.


@pytest.mark.parametrize("col,dialogue", list(enumerate(local_ml.DIALOGUE_COLS)))
@pytest.mark.parametrize("row,narration", list(enumerate(local_ml.NARRATION_ROWS)))
def test_markup_from_logits_reads_every_cell_row_major(row, narration, col, dialogue):
    grid = [0.0] * 9
    grid[row * 3 + col] = 9.0
    assert local_ml.markup_from_logits(grid) == (narration, dialogue)


def test_markup_labels_are_marginals_not_the_top_cell():
    grid = [0.0] * 9
    grid[0 * 3 + 1] = 3.0  # asterisk/bare: the single top cell
    grid[1 * 3 + 0] = grid[1 * 3 + 2] = 2.6  # bare narration, spread across two cells
    assert local_ml.markup_from_logits(grid) == ("bare", "bare")


async def test_aclassify_markup_reads_the_shaped_message_off_nine_cells(monkeypatch):
    calls: list[tuple[str, str, int]] = []

    def fake(feature: str, text: str, n: int) -> list[float]:
        calls.append((feature, text, n))
        grid = [0.0] * n
        grid[1 * 3 + 0] = 9.0  # bare / quoted
        return grid

    monkeypatch.setattr(local_ml, "_head_logits", fake)
    text = 'She **really** smiles. "Hi."\n* a bullet'
    assert await local_ml.aclassify_markup(text) == ("bare", "quoted")
    assert calls == [("markup_classifier", local_ml.markup_input(text), 9)]


async def test_aclassify_markup_never_loads_the_model_for_nothing_to_read(monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("the model must not be reached for empty shaped input")

    monkeypatch.setattr(local_ml, "_head_logits", boom)
    for text in ("", "  \n ", "```\nonly a fence\n```"):
        assert await local_ml.aclassify_markup(text) == ("unknown", "unknown")
