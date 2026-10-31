"""Autocomplete: the pure prompt trimmer -- no model, no DB.

The real-weights smoke test lived here too, but it loaded the GGUF for ~10s to
assert the output was a non-empty string; the trimmer is what Orb actually owns.
"""

import asyncio

from backend.features import autocomplete as lc


def test_build_prompt_ends_at_draft_and_excludes_injection():
    p = lc.build_prompt(
        "Aria",
        "Sam",
        "Aria is a wry tavern keeper.",
        [{"role": "assistant", "content": "You look lost."}, {"role": "user", "content": "Maybe I am."}],
        "I walk into the",
    )
    assert p.endswith("Sam: I walk into the")  # model continues this exact line
    assert "Aria: You look lost." in p
    assert "Sam: Maybe I am." in p
    assert "Aria is a wry tavern keeper." in p
    # Lightweight typeahead -- the Director/pipeline injection block must not leak in.
    assert "Director" not in p and "Scene Direction" not in p


def test_build_prompt_truncates_long_message():
    p = lc.build_prompt("A", "U", "", [{"role": "user", "content": "x" * 2000}], "hi")
    assert "x" * 501 not in p  # capped at max_msg_chars=500


def test_build_prompt_strips_card_markup_before_the_tail():
    story = 'She laughs. "Onii-san, that\'s so retro." <span class="x">She takes your hand.</span>'
    trailer = (
        "\n\n```\nPosition: standing\nMood: happy\n```\n\n<!-- RESPONSE END: location? porch. -->"
        " <img src='https://example.dev/?location=porch'> <audio controls src='x'></audio>"
        "<style>.a{display:none}</style>"
    )
    p = lc.build_prompt("A", "U", "", [{"role": "assistant", "content": story + trailer * 5}], "I")
    assert p == 'A: She laughs. "Onii-san, that\'s so retro." She takes your hand.\nU: I'
    # A message that is all markup drops out like an empty one; an unclosed comment or fence runs to the end.
    assert lc.build_prompt("A", "U", "", [{"role": "assistant", "content": "```\nHP: 3\n```"}], "go") == "U: go"
    assert lc.build_prompt("A", "U", "", [{"role": "assistant", "content": "Hi.\n<!-- cut off"}], "go") == "A: Hi.\nU: go"
    assert lc.build_prompt("A", "U", "", [{"role": "assistant", "content": "Hi.\n```\nHP: 3"}], "go") == "A: Hi.\nU: go"
    # Prose that merely looks like markup is kept.
    assert lc.build_prompt("A", "U", "", [{"role": "user", "content": "I <3 you, x < y"}], "go") == "U: I <3 you, x < y\nU: go"


def test_build_prompt_skips_empty_summary_and_messages():
    assert lc.build_prompt("A", "U", "  ", [{"role": "user", "content": "  "}], "go") == "U: go"


def test_complete_reconciles_trailing_space(monkeypatch):
    """The model garbles a whitespace-ending prompt, so complete() rstrips before
    generating; when it trimmed, it lstrips the completion so it rejoins the
    frontend's untrimmed draft without doubling the separator space."""
    seen: dict[str, str] = {}

    async def fake_acomplete(feature, prompt, *args, **kwargs):
        seen["prompt"] = prompt
        return " hands"  # model re-emits a leading word separator

    monkeypatch.setattr("backend.inference.local_ml.acomplete", fake_acomplete)

    # Trailing space: prompt trimmed before generation, leading space dropped (the user already typed the separator).
    out = asyncio.run(lc.complete("Sam: I hold up both "))
    assert seen["prompt"] == "Sam: I hold up both"  # no trailing space reaches the model
    assert out == "hands"

    # No trailing space: completion passes through untouched -- its leading space is the separator the user hasn't typed yet.
    out = asyncio.run(lc.complete("Sam: I hold up both"))
    assert seen["prompt"] == "Sam: I hold up both"
    assert out == " hands"
