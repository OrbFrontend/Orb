"""The decision definition contract: what is savable, and what a card may carry.

These tests cover the trust boundary in both directions -- the authoring API's
validator and the untrusted-card decoder -- because the plan requires them to
agree: a card must not be able to express a definition the editor would reject.
"""

from __future__ import annotations

from backend.core import (
    DECISION_COLUMNS,
    DEFAULT_STATE_TEMPLATE,
    decision_definition_errors,
    is_decision_row,
    parse_decision_definition,
)
from backend.database import card_embedded_fragments
from backend.database.seeds import SEED_INTERACTIVE_FRAGMENTS
from backend.pipeline.passes.judge import definition_problems


def _row(**overrides) -> dict:
    row = {
        "id": "outcome",
        "label": "Outcome",
        "injection_label": "Outcome",
        "field_type": "decision",
        "cooldown_turns": 0,
        "decision_type": "noul",
        "decision_placement": "before_director",
        "decision_state_template": DEFAULT_STATE_TEMPLATE,
        "decision_instructions": "Does Alric prevail in this exchange?",
        "decision_criteria": {"true": "Alric ends in control.", "false": "Alric is driven back."},
        "decision_outputs": {"true": "Alric holds the doorway.", "false": ""},
        "decision_resolution": "threshold",
        "decision_threshold": 0.5,
    }
    row.update(overrides)
    return row


def _card(entries) -> dict:
    return {"id": "card-1", "extensions": {"orb": {"fragments": {"interactive": entries}}}}


def _card_entry(**overrides) -> dict:
    entry = {key: value for key, value in _row().items() if key not in ("cooldown_turns", "injection_label")}
    entry.update(overrides)
    return entry


# ── the definition contract ──────────────────────────────────────────────────


def test_a_complete_definition_parses():
    definition = parse_decision_definition(_row())
    assert definition is not None
    assert definition.fragment_id == "outcome"
    assert definition.resolution == "threshold"
    assert definition.threshold == 0.5
    # An empty output is a real authored value: "nothing to add for this outcome".
    assert definition.outputs["false"] == ""


def test_criteria_need_both_outcomes_and_neither_may_be_empty():
    assert "decision_criteria" in " ".join(decision_definition_errors(_row(decision_criteria={"true": "only"})))
    assert "decision_criteria" in " ".join(decision_definition_errors(_row(decision_criteria={"true": "a", "false": " "})))
    # Outputs are the mirror image: both keys required, either may be empty.
    assert decision_definition_errors(_row(decision_outputs={"true": "", "false": ""})) == []
    assert "decision_outputs" in " ".join(decision_definition_errors(_row(decision_outputs={"true": "a"})))


def test_threshold_is_required_in_threshold_mode_and_forbidden_in_roll_mode():
    assert "decision_threshold" in " ".join(decision_definition_errors(_row(decision_threshold=None)))
    assert "decision_threshold" in " ".join(decision_definition_errors(_row(decision_threshold=1.5)))
    assert decision_definition_errors(_row(decision_resolution="roll", decision_threshold=None)) == []
    # A stored threshold roll mode would ignore makes two different policies look
    # identical in the Inspector and in the resolution fingerprint.
    assert "decision_threshold" in " ".join(
        decision_definition_errors(_row(decision_resolution="roll", decision_threshold=0.5))
    )


def test_boundary_thresholds_are_valid():
    for value in (0.0, 1.0):
        assert decision_definition_errors(_row(decision_threshold=value)) == []


def test_unknown_variants_are_rejected_rather_than_defaulted():
    for field, value in (
        ("decision_type", "unknown"),
        ("decision_placement", "after_director"),
        ("decision_resolution", "weighted"),
    ):
        assert parse_decision_definition(_row(**{field: value})) is None, field


def test_choice_and_score_derive_their_outcome_spaces():
    choice = parse_decision_definition(
        _row(
            decision_type="choice",
            decision_criteria={"clean": "Clean win", "messy": "Messy win"},
            decision_outputs={"clean": "Clean", "messy": "Messy"},
            decision_resolution="argmax",
            decision_threshold=None,
            decision_confidence_floor=0.5,
        )
    )
    assert choice is not None
    assert choice.outcome_keys == ("clean", "messy")

    score = parse_decision_definition(
        _row(
            decision_type="score",
            decision_criteria=["Low", "High"],
            decision_outputs={"0": "Low", "1": "High"},
            decision_resolution="nearest",
            decision_threshold=None,
        )
    )
    assert score is not None
    assert score.outcome_keys == ("0", "1")


def test_noul_criteria_are_normalized_to_outcome_order():
    # The canonical question form identifies a question for caching, so two
    # definitions differing only in dict order must ask the same question.
    definition = parse_decision_definition(_row(decision_criteria={"false": "b", "true": "a"}))
    assert definition is not None and list(definition.criteria) == ["true", "false"]


def test_criteria_may_arrive_as_json_text():
    row = _row(decision_criteria='{"true": "a", "false": "b"}')
    definition = parse_decision_definition(row)
    assert definition is not None and definition.criteria == {"true": "a", "false": "b"}
    assert parse_decision_definition(_row(decision_criteria="not json")) is None


def test_every_error_is_reported_at_once():
    problems = decision_definition_errors(
        _row(decision_type="", decision_instructions="", decision_state_template="", decision_resolution="")
    )
    assert len(problems) >= 4


def test_a_non_decision_row_never_parses():
    assert not is_decision_row(_row(field_type="string"))
    assert parse_decision_definition(_row(field_type="string")) is None


# ── the authoring validator adds the renderer's macro set ────────────────────


def test_authoring_rejects_a_macro_the_renderer_cannot_supply():
    problems = definition_problems(_row(decision_state_template="Judge {{scene_guidance}}"))
    assert any("scene_guidance" in problem for problem in problems)
    assert any("before the Director" in problem for problem in problems)
    assert definition_problems(_row(decision_state_template="Judge {{nonsense}}"))


def test_authoring_rejects_unsupported_macros_in_question_text():
    # The question is a cache-key and fingerprint input, so it takes the narrow
    # identity set only -- a per-turn macro there would fragment the cache.
    assert definition_problems(_row(decision_instructions="Does {{last_message}} succeed?"))
    assert definition_problems(_row()) == []
    assert definition_problems(_row(decision_instructions="Does {{char}} prevail against {{user}}?")) == []


def test_authoring_accepts_the_default_template():
    assert definition_problems(_row(decision_state_template=DEFAULT_STATE_TEMPLATE)) == []


# ── the untrusted-card decoder ───────────────────────────────────────────────


def test_a_valid_card_decision_decodes_with_its_columns():
    _, interactive = card_embedded_fragments(_card([_card_entry()]))
    assert len(interactive) == 1
    assert interactive[0]["field_type"] == "decision"
    assert parse_decision_definition(interactive[0]) is not None


def test_an_unknown_card_variant_is_skipped_not_demoted_to_a_string_field():
    # The rule the plan states outright: a card naming a decision type Orb does
    # not implement contributes nothing, rather than a fragment that silently
    # means something else and joins the Director's tool schema.
    _, interactive = card_embedded_fragments(_card([_card_entry(decision_type="score")]))
    assert interactive == []


def test_a_malformed_card_decision_is_skipped():
    for broken in (
        {"decision_criteria": {"true": "only"}},
        {"decision_instructions": ""},
        {"decision_outputs": "junk"},
        {"decision_threshold": "high"},
    ):
        _, interactive = card_embedded_fragments(_card([_card_entry(**broken)]))
        assert interactive == [], broken


def test_non_decision_card_fragments_still_carry_null_decision_columns():
    _, interactive = card_embedded_fragments(_card([{"id": "pacing", "label": "Pacing", "field_type": "string"}]))
    assert all(interactive[0][column] is None for column in DECISION_COLUMNS)


def test_every_seeded_decision_is_a_valid_definition():
    seeded = [row for row in SEED_INTERACTIVE_FRAGMENTS if is_decision_row(row)]
    assert seeded
    for row in seeded:
        assert definition_problems(row) == [], row["id"]
