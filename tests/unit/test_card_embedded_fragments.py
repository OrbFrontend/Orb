"""Unit tests for card_embedded_fragments — the trust boundary that turns a
card's extensions.orb.fragments (arbitrary imported-PNG JSON) into fragment-row
shapes the pipeline can consume."""

from __future__ import annotations

from backend.database import card_embedded_fragments


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
            "enabled": 1,
        }
    ]
    assert interactive == [
        {
            "id": "trust",
            "label": "Trust",
            "description": "how much",
            "field_type": "progressive",
            "required": 1,
            "enabled": 1,
            "injection_label": "Trust level",
            "sort_order": 10_000,
            "direction_note_timing": "pre_writer",
        }
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


def test_missing_or_blank_label_skipped():
    moods, _ = card_embedded_fragments(_card({"mood": [{"id": "a"}, {"id": "b", "label": "  "}, {"id": "c", "label": 7}]}))
    assert moods == []


def test_unknown_enums_fall_back():
    _, interactive = card_embedded_fragments(
        _card({"interactive": [{"id": "a", "label": "A", "field_type": "banana", "direction_note_timing": "whenever"}]})
    )
    assert interactive[0]["field_type"] == "string"
    assert interactive[0]["direction_note_timing"] == "post_turn"


def test_namespaced_provider_and_type_config_are_preserved_without_installation():
    _, interactive = card_embedded_fragments(
        _card(
            {
                "interactive": [
                    {
                        "id": "trust",
                        "label": "Trust",
                        "field_type": "scene-meter:meter",
                        "type_config": {"minimum": 0, "maximum": 100, "initial": 40, "max_delta": 10},
                    }
                ]
            }
        )
    )
    assert interactive[0]["field_type"] == "scene-meter:meter"
    assert interactive[0]["type_config"] == '{"minimum":0,"maximum":100,"initial":40,"max_delta":10}'


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


def test_sort_order_offsets_by_array_position():
    _, interactive = card_embedded_fragments(_card({"interactive": [{"id": "a", "label": "A"}, {"id": "b", "label": "B"}]}))
    assert [f["sort_order"] for f in interactive] == [10_000, 10_001]
