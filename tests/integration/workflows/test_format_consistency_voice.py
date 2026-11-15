"""The povtense head's read-out and the voice check's addressee comparison. Pure functions: no model, no network."""

import pytest

from backend.inference import local_ml
from backend.workflows.format_consistency import voice

# The v3 head's logit order: 6 voice rows x 3 tense columns, row-major. A misordered reading still returns plausible labels, so
# every cell is pinned here.
_CELLS = [
    (pov, tense, addressee)
    for pov, addressee in (
        ("first", "you"),
        ("first", "other"),
        ("first", "none"),
        ("second", "you"),
        ("third", "other"),
        ("ambiguous", "none"),
    )
    for tense in ("past", "present", "ambiguous")
]


@pytest.mark.parametrize(("cell", "labels"), list(enumerate(_CELLS)))
def test_each_one_hot_cell_reads_as_its_triple(cell, labels):
    logits = [0.0] * len(_CELLS)
    logits[cell] = 20.0
    assert local_ml._voice_from_logits(logits) == labels


def test_the_pov_is_summed_over_the_addressee_split():
    # first|you, first|other and first|none each lose to `third` alone, and win once summed.
    logits = [0.0] * len(_CELLS)
    for cell in (1, 4, 7):  # first|{you,other,none}, present
        logits[cell] = 5.0
    logits[13] = 5.6  # third, present
    assert local_ml._voice_from_logits(logits)[:2] == ("first", "present")


@pytest.mark.parametrize(("other_logit", "labels"), [(1.0, ("first", "present", "none")), (2.6, ("first", "present", "other"))])
def test_a_weak_other_reads_none(other_logit, labels):
    # first|other present against first|none present: 0.73 of the mass is under the floor, 0.93 is over it.
    logits = [-10.0] * len(_CELLS)
    logits[4], logits[7] = other_logit, 0.0
    assert local_ml._voice_from_logits(logits) == labels


@pytest.mark.parametrize(("source", "target"), [("other", "you"), ("you", "other")])
def test_drift_reports_an_addressee_switch_between_first_person_readings(source, target):
    phrases = voice.drift(("first", "past", source), ("first", "past", target))
    assert len(phrases) == 1


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (("first", "past", "none"), ("first", "past", "you")),
        (("first", "past", "you"), ("first", "past", "none")),
        # Outside first person the addressee follows from the POV, which the POV phrase already covers.
        (("third", "past", "you"), ("third", "past", "other")),
    ],
)
def test_drift_is_silent_on_the_addressee_when_it_cannot_compare(source, target):
    assert voice.drift(source, target) == []


def test_combine_takes_the_addressee_from_every_window():
    # A `second` window addresses "you" whatever the first-person windows say.
    windows = [("first", "past", "other"), ("first", "past", "none"), ("second", "past", "you")]
    assert voice.combine(windows) == ("first", "past", "you")
