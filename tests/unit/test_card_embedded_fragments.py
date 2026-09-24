"""Unit tests for card_embedded_fragments — the trust boundary that turns a
card's extensions.orb.fragments (arbitrary imported-PNG JSON) into fragment-row
shapes the pipeline can consume."""

from __future__ import annotations

from backend.core import DECISION_COLUMNS, STATE_COLUMNS
from backend.database import card_embedded_fragments

# Every interactive row carries the decision columns, null for the types that do
# not use them, so one reader can ask any fragment whether it is a decision
# without first asking what kind of fragment it is.
NO_DECISION = {column: None for column in DECISION_COLUMNS}
NO_STATE = {column: None for column in STATE_COLUMNS}


def _card(frags):
    return {"extensions": {"orb": {"fragments": frags}}}


def test_happy_path_shapes():
    moods, interactive = card_embedded_fragments(
        _card(
            {
                "mood": [
                    {
                        "id": "brooding",
                        "label": "Brooding",
                        "description": "dark",
                        "prompt_text": "be moody",
                        "negative_prompt": "stop moping",
                        "cooldown_turns": 4,
                        "enabled": True,
                    }
                ],
                "interactive": [
                    {
                        "id": "trust",
                        "label": "Trust",
                        "description": "how much",
                        "field_type": "progressive",
                        "required": True,
                        "injection_label": "Trust level",
                        "direction_note_timing": "pre_writer",
                        "cooldown_turns": 7,
                    }
                ],
            }
        )
    )
    assert moods == [
        {
            "id": "brooding",
            "label": "Brooding",
            "description": "dark",
            "prompt_text": "be moody",
            "negative_prompt": "stop moping",
            "cooldown_turns": 4,
            "enabled": 1,
        }
    ]
    # A legacy progressive fragment is read as the state fragment it converts to:
    # one value, updated before the Writer, injected into both passes.
    assert interactive == [
        {
            "id": "trust",
            "label": "Trust",
            "description": "how much",
            "field_type": "state",
            "required": 1,
            "enabled": 1,
            "injection_label": "Trust level",
            "sort_order": 10_000,
            "direction_note_timing": "post_turn",
            "cooldown_turns": 7,
            "state_mode": "value",
            "state_update": "before_writer",
            "state_inject": "both",
            **NO_DECISION,
        }
    ]


def test_legacy_direction_note_maps_to_multiple_entries_on_its_own_timing():
    _, interactive = card_embedded_fragments(
        _card(
            {
                "interactive": [
                    {"id": "pre", "label": "Pre", "field_type": "direction_note", "direction_note_timing": "pre_writer"},
                    {"id": "post", "label": "Post", "field_type": "direction_note"},
                ]
            }
        )
    )
    # Cards never carried an inject setting, so converted notes inject into both.
    assert [(f["field_type"], f["state_mode"], f["state_update"], f["state_inject"]) for f in interactive] == [
        ("state", "entries", "before_writer", "both"),
        ("state", "entries", "after_reply", "both"),
    ]


def test_state_settings_are_read_and_unknown_values_fall_back():
    _, interactive = card_embedded_fragments(
        _card(
            {
                "interactive": [
                    {
                        "id": "threads",
                        "label": "Threads",
                        "field_type": "state",
                        "state_mode": "entries",
                        "state_update": "manual",
                        "state_inject": "writer",
                    },
                    {
                        "id": "odd",
                        "label": "Odd",
                        "field_type": "state",
                        "state_mode": "several",
                        "state_update": "sometimes",
                        "state_inject": 3,
                    },
                    {"id": "plain", "label": "Plain", "field_type": "string", "state_mode": "entries"},
                ]
            }
        )
    )
    assert [(f["state_mode"], f["state_update"], f["state_inject"]) for f in interactive] == [
        ("entries", "manual", "writer"),
        ("value", "after_reply", "both"),
        (None, None, None),
    ]


def test_malformed_levels_yield_empty():
    for card in (
        None,
        {},
        {"extensions": None},
        {"extensions": "junk"},
        {"extensions": {"orb": "junk"}},
        {"extensions": {"orb": {"fragments": [1, 2]}}},
        _card({"mood": "junk", "interactive": {"a": 1}}),
        _card({"mood": ["junk", 42, None]}),
    ):
        assert card_embedded_fragments(card) == ([], [])


def test_invalid_ids_skipped():
    moods, _ = card_embedded_fragments(
        _card(
            {
                "mood": [
                    {"id": "has space", "label": "A", "prompt_text": "p"},
                    {"id": "UPPER", "label": "B", "prompt_text": "p"},
                    {"id": "", "label": "C", "prompt_text": "p"},
                    {"id": 5, "label": "D", "prompt_text": "p"},
                    {"id": "x" * 65, "label": "E", "prompt_text": "p"},
                    {"id": "fine_id-2", "label": "F", "prompt_text": "p"},
                ]
            }
        )
    )
    assert [f["id"] for f in moods] == ["fine_id-2"]


def test_reserved_interactive_ids_skipped():
    # `moods` and `retire` are fixed tool parameters a fragment would overwrite.
    _, interactive = card_embedded_fragments(
        _card(
            {
                "interactive": [
                    {"id": "moods", "label": "A", "description": "d"},
                    {"id": "retire", "label": "B", "description": "d", "field_type": "state"},
                    {"id": "threads", "label": "C", "description": "d", "field_type": "state"},
                ]
            }
        )
    )
    assert [f["id"] for f in interactive] == ["threads"]


def test_missing_or_blank_label_skipped():
    moods, _ = card_embedded_fragments(_card({"mood": [{"id": "a"}, {"id": "b", "label": "  "}, {"id": "c", "label": 7}]}))
    assert moods == []


def test_unknown_enums_fall_back():
    _, interactive = card_embedded_fragments(
        _card({"interactive": [{"id": "a", "label": "A", "field_type": "banana", "direction_note_timing": "whenever"}]})
    )
    assert interactive[0]["field_type"] == "string"
    assert {column: interactive[0][column] for column in STATE_COLUMNS} == NO_STATE


def test_post_processing_field_type_is_preserved():
    _, interactive = card_embedded_fragments(
        _card({"interactive": [{"id": "humanize", "label": "Humanize", "field_type": "post_processing"}]})
    )
    assert interactive[0]["field_type"] == "post_processing"


def test_duplicate_ids_first_wins():
    moods, _ = card_embedded_fragments(
        _card({"mood": [{"id": "a", "label": "First", "prompt_text": "p"}, {"id": "a", "label": "Second"}]})
    )
    assert len(moods) == 1
    assert moods[0]["label"] == "First"


def test_disabled_skipped():
    moods, interactive = card_embedded_fragments(
        _card(
            {
                "mood": [{"id": "a", "label": "A", "enabled": False}, {"id": "b", "label": "B", "enabled": 0}],
                "interactive": [{"id": "c", "label": "C", "enabled": True}],
            }
        )
    )
    assert moods == []
    assert [f["id"] for f in interactive] == ["c"]


def test_capped_at_50_per_type():
    moods, _ = card_embedded_fragments(_card({"mood": [{"id": f"m{i}", "label": "M"} for i in range(200)]}))
    assert len(moods) == 50


def test_non_string_text_fields_coerced_to_defaults():
    moods, interactive = card_embedded_fragments(
        _card(
            {
                "mood": [{"id": "a", "label": "A", "description": 42, "prompt_text": None}],
                "interactive": [{"id": "b", "label": "B", "injection_label": ["x"]}],
            }
        )
    )
    assert moods[0]["description"] == ""
    assert moods[0]["prompt_text"] == ""
    # injection_label falls back to the label when unusable
    assert interactive[0]["injection_label"] == "B"


def test_cooldown_defaults_and_clamps():
    moods, interactive = card_embedded_fragments(
        _card(
            {
                "mood": [
                    {"id": "a", "label": "A", "cooldown_turns": -2},
                    {"id": "b", "label": "B", "cooldown_turns": "9"},
                ],
                "interactive": [{"id": "c", "label": "C", "cooldown_turns": 99}],
            }
        )
    )
    assert [fragment["cooldown_turns"] for fragment in moods] == [0, 0]
    assert interactive[0]["cooldown_turns"] == 50


def test_sort_order_offsets_by_array_position():
    _, interactive = card_embedded_fragments(_card({"interactive": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}))
    assert [f["sort_order"] for f in interactive] == [10_000, 10_001]
