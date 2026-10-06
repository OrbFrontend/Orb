"""char_context: system-prompt precedence and card-derived fields.

Pins that the shared and model-specific prompts join shared-first, that a card's own system prompt replaces that join unless
prevent_prompt_overrides is set, and that no card yields empty persona/example fields.
"""

from backend.prompting import char_context


def test_card_system_prompt_replaces_the_joined_settings_prompt():
    card = {"description": "D", "personality": "P", "mes_example": "M", "system_prompt": "S"}
    assert char_context({"shared_system_prompt": "shared", "system_prompt": "base"}, card) == ("S", "D\n\nP", "M")


def test_prevent_prompt_overrides_keeps_the_settings_prompt():
    settings = {"shared_system_prompt": "shared", "system_prompt": "base", "prevent_prompt_overrides": 1}
    system_prompt, _persona, _example = char_context(settings, {"system_prompt": "S"})
    assert system_prompt == "shared\n\nbase"


def test_no_card_yields_empty_fields():
    assert char_context({"system_prompt": "base"}, None) == ("base", "", "")
