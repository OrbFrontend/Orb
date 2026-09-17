"""The generator's output contract and one-shot model request."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from backend.features.card_generator.generator import (
    FIELD_CAPS,
    GENERATE_CARD_TOOL,
    CardGenerationUnavailable,
    clean_card,
    generate_card,
)
from backend.inference import LLMClient
from backend.inference.schema import strictify_schema

DRAFT = {"name": "Mara", "first_mes": "Hello, {{user}}.\nCome inside."}


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", None),
        ("name", " \n"),
        ("name", 42),
        ("name", "x" * 101),
        ("first_mes", None),
        ("first_mes", " "),
        ("first_mes", 42),
        ("description", "{{roll::1d6}}"),
        ("scenario", "{broken}"),
        ("name", "{{char}}"),
        ("creator_notes", "{{user}}"),
    ],
)
def test_unrecoverable_fields_fail(field, value):
    with pytest.raises(CardGenerationUnavailable):
        clean_card({**DRAFT, field: value})


@pytest.mark.parametrize("field,cap", FIELD_CAPS.items())
def test_long_prose_clamps_at_last_complete_sentence(field, cap):
    sentence = "A complete sentence. "
    prefix = sentence * (cap // len(sentence))
    result = clean_card({**DRAFT, field: prefix + "Long continuation " * cap})
    assert result[field] == prefix.rstrip()
    assert len(result[field]) <= cap


@pytest.mark.parametrize("field", [key for key in FIELD_CAPS if key != "creator_notes"])
def test_allowed_macros_and_paragraphs_survive(field):
    prose = "{{CHAR}} welcomes {{user}}.\n\nA new paragraph."
    assert clean_card({**DRAFT, field: prose})[field] == prose


def test_soft_fields_coerce_missing_or_null_to_empty_and_strip_extra_keys():
    card = clean_card({**DRAFT, "mes_example": None, "description": 5, "id": "foreign", "system_prompt": "override"})
    assert card["mes_example"] == card["description"] == card["creator_notes"] == ""
    assert card["source_format"] == "generated"
    assert card["tags"] == card["alternate_greetings"] == []
    assert "id" not in card and "system_prompt" not in card


def test_braces_past_the_clamp_still_fail():
    with pytest.raises(CardGenerationUnavailable):
        clean_card({**DRAFT, "description": "Safe. " * 2000 + "{{date}}"})


def test_clamping_falls_back_to_words_then_hard_cut_without_splitting_macros():
    assert clean_card({**DRAFT, "scenario": "word " * 400})["scenario"] == ("word " * 320).rstrip()
    assert clean_card({**DRAFT, "scenario": "x" * 1700})["scenario"] == "x" * 1600
    text = "x" * 1597 + "{{char}}" + "x" * 100
    assert clean_card({**DRAFT, "scenario": text})["scenario"] == "x" * 1597


def test_all_seven_fields_remain_required_nonnullable_in_strict_schema():
    schema = strictify_schema(GENERATE_CARD_TOOL["function"]["parameters"])
    assert schema["required"] == ["name", *FIELD_CAPS]
    assert all(prop["type"] == "string" for prop in schema["properties"].values())


@pytest.mark.parametrize("thinking,budget,expected", [(False, 200, 4096), (True, 200, 8192), (False, 16000, 16000)])
async def test_budget_and_thinking_are_explicit(thinking, budget, expected):
    captured = {}

    async def complete(**kwargs):
        captured.update(kwargs)
        yield {
            "type": "done",
            "message": {
                "tool_calls": [
                    {
                        "id": "draft",
                        "type": "function",
                        "function": {
                            "name": "generate_character_card",
                            "arguments": json.dumps(DRAFT),
                        },
                    }
                ]
            },
        }

    client = Mock(spec=LLMClient)
    client.complete = complete
    result = await generate_card(
        client,
        "agent",
        'idea """ boundary',
        settings={"agent_max_tokens": budget},
        reasoning_on=thinking,
        library_digest='names """ data',
    )
    assert result["name"] == "Mara"
    assert captured["max_tokens"] == expected
    assert captured["chat_template_kwargs"]["enable_thinking"] is thinking
    assert captured["messages"][1]["content"].count('"""') == 4


async def test_no_tool_call_is_an_unavailable_draft():
    async def complete(**kwargs):
        yield {"type": "done", "message": {"content": "Here is your card!"}}

    client = Mock(spec=LLMClient)
    client.complete = complete
    with pytest.raises(CardGenerationUnavailable):
        await generate_card(client, "agent", "idea", settings={})


def test_generator_caps_fit_the_editor_limits():
    source = (Path(__file__).parents[2] / "frontend" / "validate.js").read_text()
    import re

    limits = {key: int(value) for key, value in re.findall(r"const (MAX_CHARACTER_\w+) = (\d+);", source)}
    assert 100 <= limits["MAX_CHARACTER_NAME"]
    assert max(FIELD_CAPS.values()) <= limits["MAX_CHARACTER_FIELD"]
    assert FIELD_CAPS["creator_notes"] <= limits["MAX_CHARACTER_ADVANCED"]
