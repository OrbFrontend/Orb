"""Card scripts project one canonical body independently for prompt and display."""

from __future__ import annotations

import pytest

from backend.core import CardScripts, Macros, TurnCast
from backend.core.card_scripts import MAX_TEXT_LENGTH
from backend.prompting.base import build_prefix, format_message_with_attachments


def script(find="/secret/g", replace="visible", **kwargs):
    return {"findRegex": find, "replaceString": replace, "placement": [2], "promptOnly": True, **kwargs}


def compile_scripts(*scripts):
    return CardScripts.from_extensions({"regex_scripts": list(scripts)})


@pytest.mark.parametrize(
    ("pattern", "replacement", "source", "expected"),
    [
        ("/^a.(b)$/gims", "$1", "A\nb\naXb", "b\nb"),
        ("/(a)(b)?/g", r"$1|$2|$$|$&|$9|$12|\n", "a", "a||$|a|$9|a2|\\n"),
        ("/a/", "X", "aa", "Xa"),
        ("/a/g", "X", "aa", "XX"),
        (r"/<\/div>/g", "", "hello</div>", "hello"),
        ("/b/", "$`-$'", "abc", "aa-cc"),
    ],
)
def test_js_pattern_flags_and_replacements(pattern, replacement, source, expected):
    assert compile_scripts(script(pattern, replacement)).apply(source, "prompt", "assistant") == expected


def test_channels_roles_disabled_and_declaration_order():
    scripts = compile_scripts(
        script("/secret/g", "one"),
        script("/one/g", "two", markdownOnly=True),
        script("/two/g", "wrong", disabled=True),
        script("/secret/g", "reader", promptOnly=False),
        script("/secret/g", "user", placement=[1]),
    )
    assert scripts.apply("secret", "prompt", "assistant") == "two"
    assert scripts.apply("secret", "display", "assistant") == "reader"
    assert scripts.apply("secret", "prompt", "user") == "user"
    assert scripts.apply("secret", "prompt", "system") == "secret"


@pytest.mark.parametrize("bad", ["/[broken/g", "/a/gg", "/a/y", "/missing"])
def test_bad_pattern_skipped_without_losing_valid_scripts(bad, caplog):
    scripts = compile_scripts(script(bad), script())
    assert scripts.apply("secret", "prompt", "assistant") == "visible"
    assert "card regex" in caplog.text


@pytest.mark.parametrize("raw", [None, [], {"regex_scripts": {}}, {"regex_scripts": [None, {}, {"placement": 2}]}])
def test_malformed_extensions_are_noop(raw):
    assert CardScripts.from_extensions(raw).apply("secret", "prompt", "assistant") == "secret"


def test_per_card_disable_does_not_mutate_declarations():
    extensions = {"regex_scripts": [script()], "orb": {"card_scripts_enabled": False}}
    assert CardScripts.from_extensions(extensions).apply("secret", "prompt", "assistant") == "secret"
    assert extensions["regex_scripts"] == [script()]


def test_timeout_restores_original_even_after_earlier_script(caplog):
    scripts = compile_scripts(script("/start/g", "changed"), script("/(a+)+$/g", "bad"))
    text = "start " + "a" * 20_000 + "!"
    assert scripts.apply(text, "prompt", "assistant") == text
    assert "timed out" in caplog.text


def test_input_and_output_caps_restore_original():
    scripts = compile_scripts(script("/a/g", "aa"))
    text = "a" * (MAX_TEXT_LENGTH + 1)
    assert scripts.apply(text, "prompt", "assistant") == text
    text = "a" * 60_000
    assert scripts.apply(text, "prompt", "assistant") == text


def test_greeting_prompt_projection_macros_first_and_attachments_untouched():
    scripts = compile_scripts(
        script(r'/<div id="user-only"(.*?)\n?<\/div>\n*?/igs', ""),
        script(r'/<div id="llm-only" .*?>(.*?)<\/div>/igs', "$1"),
        script("/Amy/g", "the narrator"),
    )
    source = '<div id="user-only">Reader artifact</div><div id="llm-only" hidden>**{{char}}** speaks.</div><!-- plot note -->'
    message = {"role": "assistant", "content": source, "workflow_attachments": [{"annotation": "Amy attachment"}]}
    rendered = format_message_with_attachments(message, Macros("User", "Amy"), scripts)
    assert rendered["content"] == "**the narrator** speaks.<!-- plot note -->\n\nAmy attachment"
    assert message["content"] == source


def test_group_history_uses_own_speaker_scripts_and_leaves_summaries_alone():
    scripts = compile_scripts(script())
    prefix = build_prefix(
        "system",
        "",
        "",
        messages=[
            {"role": "assistant", "content": "secret", "speaker_member_id": "a"},
            {"role": "assistant", "content": "secret", "speaker_member_id": "b"},
            {"role": "assistant", "content": "secret"},
        ],
        cast=TurnCast(True, ()),
        speaker_names={"a": "A", "b": "B"},
        speaker_scripts={"a": scripts},
    )
    assert prefix[1]["content"] == "A: visible\n\nB: secret\n\nSummary: secret"
