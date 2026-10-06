"""Unit tests for the inline macro engine (backend/core/macros.py).

Covers the {{random::a::b}} grammar, the fresh-roll persist-boundary entry (resolve_inline), the per-conversation choice map
(resolve_stored_random), the seeded Macros determinism used for per-turn-rebuilt prompt fields, and the idempotency invariant
the persist boundary relies on (resolving already resolved text is a no-op).
"""

import time

import pytest

from backend.core.macros import (
    Macros,
    card_description,
    has_inline_macros,
    resolve_inline,
    resolve_message,
    resolve_stored_random,
)

# -- grammar / resolve_inline -------------------------------------------------


_INLINE = {
    "random_single_option_is_deterministic": ("go {{random::north}}", "go north"),
    "random_empty_options_resolve_to_empty_string": ("{{random::}}", ""),
    "random_case_insensitive": ("{{RANDOM::up}}{{Random::up}}", "upup"),
    "random_multiline_options": ("{{random::line1\nline2}}", "line1\nline2"),
    # Two macros on one line must not merge into one greedy match.
    "random_non_greedy_terminates_at_first_close": ("{{random::a}} and {{random::b}}{{random::c}}", "a and bc"),
    "roll_still_fires_and_random_leaves_user_char_alone": (
        "{{roll::2d1}} {{random::x}} {{user}} {{char}}",
        "2 x {{user}} {{char}}",
    ),
    "empty": ("", ""),
    # A lone backtick opens no span; spans don't cross newlines.
    "unpaired_backtick_does_not_escape": ("` {{random::a}}", "` a"),
    "multiline_backticks_do_not_escape": ("`no close\n{{random::a}}`", "`no close\na`"),
    "pick_is_random_alias": ("{{PICK::up}}", "up"),
    "date_in_backticks_stays_literal": ("say `{{date}}`", "say `{{date}}`"),
    # A comment on its own line leaves no blank line behind.
    "comment_stripped_with_its_line": ("a\n{{// note to self}}\nb", "a\nb"),
    "comment_inline": ("keep {{// drop}}this", "keep this"),
    "comment_multiline_and_repeated": (
        "# SHEET\n{{//\n- all start at 1\n- max 5\n}}\n## PHYSICAL\n{{// second }}\nStrength: 1",
        "# SHEET\n## PHYSICAL\nStrength: 1",
    ),
    # Comments are stripped before every other inline macro, so a nested macro is deleted rather than resolved.
    "macro_inside_comment_does_not_fire": ("{{// dice: {{roll::1d6}} }}x", "x"),
    # The comment closes on its own `}}`, not the nested macro's.
    "comment_body_may_contain_a_nested_macro": ("{{// see {{user}} }}", ""),
    "comment_body_nested_with_tail": ("{{// a {{b}} c }}tail", "tail"),
    "comment_body_nested_multiline": ("{{// a\nb {{user}} c\n}}", ""),
    # Closing on the nested macro's `}}` left the rest of the note in the stored row.
    "comment_does_not_publish_the_tail_of_a_note": ("Note {{// remember {{user}} likes tea }} ok", "Note  ok"),
    # Documented limit: a brace-free nested form keeps the scan linear, so two-deep nesting still closes early.
    "comment_body_ends_early_when_nested_two_deep": ("{{// deep {{a {{b}} c}} }}", " }}"),
    # A line-opening comment takes itself, never the writing (or the lines) after it.
    "comment_does_not_eat_the_prose_that_follows_it": ("{{// note }}Hello {{user}}\nBody", "Hello {{user}}\nBody"),
    "comment_does_not_eat_a_plain_tail": ("{{// note }}plain tail\nBody", "plain tail\nBody"),
    "comment_does_not_eat_following_lines": ("{{// note }}one\ntwo {{char}}\nthree", "one\ntwo {{char}}\nthree"),
    "comment_does_not_eat_to_a_later_close": ("{{// note }}x\nmore\ny}}\ntail", "x\nmore\ny}}\ntail"),
    # Several comments alone on a line still take the line, spacing included.
    "run_of_comments_owns_its_line": ("{{// a }}{{// b }}\nBody", "Body"),
    "spaced_run_of_comments_owns_its_line": ("  {{// a }} {{// b }}  \nBody", "Body"),
    # `\r` is not horizontal whitespace, so CRLF own-line comments must still take their line.
    "comment_owns_its_line_in_a_crlf_card": ("a\r\n{{// note }}\r\nb", "a\r\nb"),
    "comment_run_in_a_crlf_card": ("{{// a }}{{// b }}\r\nBody", "Body"),
    "trim_joins_across_newlines": ("a\n{{trim}}\nb", "ab"),
    "trim_joins_across_many_newlines": ("a\n\n\n{{trim}}\n\nb", "ab"),
    "trim_inline": ("x{{trim}}y", "xy"),
    # Card fields commonly arrive CRLF; \r must go with the \n it belongs to.
    "trim_eats_crlf_newlines": ("a\r\n{{trim}}\r\n\r\nb", "ab"),
    "trim_at_start": ("{{trim}}\n\nBody", "Body"),
    "trim_at_end": ("Body\n\n{{trim}}", "Body"),
    "trim_case_insensitive_and_repeated": ("a\n{{TRIM}}\nb\n{{Trim}}\nc", "abc"),
    # The card idiom: a header comment followed by {{trim}}; the comment's line branch must not swallow the trim.
    "trim_after_comment": ("{{// note }}{{trim}}\n\nBody", "Body"),
    "trim_after_comment_crlf": ("{{// note }}{{trim}}\r\n\r\nBody", "Body"),
    "trim_on_the_line_after_a_comment": ("{{// note }}\n{{trim}}\nBody", "Body"),
    "trim_after_an_own_line_comment": ("A\n{{// note }}\n{{trim}}B", "AB"),
    "trim_leaves_horizontal_whitespace_alone": ("a \n{{trim}}\n b", "a  b"),
    "backticked_trim_stays_literal": ("write `{{trim}}` to join lines", "write `{{trim}}` to join lines"),
    # {{description}} resolves on read like {{user}}, so the persist boundary stores it raw.
    "description_is_not_an_inline_macro": ("{{description}}", "{{description}}"),
    "roll_with_no_sides_is_left_raw": ("{{roll::1d0}}", "{{roll::1d0}}"),
    "roll_with_too_many_dice_is_left_raw": ("{{roll::1001d6}}", "{{roll::1001d6}}"),
    "roll_at_the_dice_limit": ("{{roll::1000d1}}", "1000"),
    "roll_of_no_dice": ("{{roll::0d6}}", "0"),
}


@pytest.mark.parametrize("text,expected", _INLINE.values(), ids=_INLINE.keys())
def test_resolve_inline(text, expected):
    assert resolve_inline(text) == expected


def test_random_picks_a_member():
    assert resolve_inline("{{random::red::blue}}") in {"red", "blue"}
    assert resolve_inline("x{{random::a::}}y") in {"xay", "xy"}
    assert resolve_inline("{{pick::red::blue}}") in {"red", "blue"}
    assert resolve_inline(None) == ""  # type: ignore[arg-type]


# -- has_inline_macros --------------------------------------------------------


def test_has_inline_macros():
    assert has_inline_macros("hi {{random::a::b}}")
    assert has_inline_macros("hi {{roll::2d6}}")
    assert not has_inline_macros("hi {{user}}, meet {{char}}")
    assert not has_inline_macros("plain text")
    assert not has_inline_macros("")


# -- idempotency (the persist-boundary invariant) -----------------------------


def test_resolve_message_idempotent_on_resolved_text():
    once = resolve_message("{{user}} rolls {{roll::3d1}} and picks {{random::only}} for {{char}}", "Alice", "Bot")
    assert once == "Alice rolls 3 and picks only for Bot"
    assert resolve_message(once, "Alice", "Bot") == once


# -- seeded determinism (per-turn-rebuilt prompt fields) ----------------------


def test_seeded_macros_are_deterministic():
    m = Macros("Alice", "Bot", seed="conv-1")
    text = "sky is {{random::red::green::blue}} and sea is {{random::red::green::blue}}"
    first = m.resolve_message(text)
    assert first == m.resolve_message(text)
    assert "{{random" not in first


def test_seeded_pick_survives_surrounding_edits():
    # The ordinal keys on the macro's own text, so unrelated prose changes around it must not re-roll the pick.
    m = Macros("A", "B", seed="conv-2")
    pick = m.resolve_message("{{random::sun::rain::fog}}")
    assert m.resolve_message("Today: {{random::sun::rain::fog}}, allegedly.") == f"Today: {pick}, allegedly."


def test_unseeded_macros_still_resolve():
    m = Macros("Alice", "Bot")
    assert m.seed == ""
    assert m.resolve_message("{{random::l::r}}") in {"l", "r"}


def test_seeded_roll_is_deterministic():
    # A {{roll}} in per-turn-rebuilt text (persona: "a monster with {{roll::3d8}}
    # limbs") must resolve to the same bytes every turn of the conversation.
    m = Macros("Alice", "Bot", seed="conv-3")
    text = "the beast has {{roll::3d8}} limbs and {{roll::3d8}} eyes"
    first = m.resolve_message(text)
    assert first == m.resolve_message(text)
    assert "{{roll" not in first
    # Different seed = an independent conversation rolls its own dice.
    assert len({Macros("A", "B", seed=f"conv-{i}").resolve_message("{{roll::10d100}}") for i in range(20)}) > 1


def test_unseeded_roll_rolls_fresh():
    # 20 draws of 10d100 all landing on the same total would mean the RNG froze.
    assert len({resolve_inline("{{roll::10d100}}") for _ in range(20)}) > 1


# -- resolve_stored_random (per-conversation choice map) ----------------------


def test_stored_random_records_and_reuses():
    choices: dict[str, str] = {}
    (first,) = resolve_stored_random(["{{random::crimson::azure}}"], choices, "mood:m1")
    assert choices == {"mood:m1:{{random::crimson::azure}}:0": first}
    (again,) = resolve_stored_random(["{{random::crimson::azure}}"], dict(choices), "mood:m1")
    assert again == first


def test_stored_random_keys_by_macro_text_and_ordinal():
    # Distinct macros key independently; a repeat of the same macro gets its own ordinal; keys span all texts of the call.
    choices: dict[str, str] = {}
    resolve_stored_random(["{{random::a::b}} {{random::c::d}}", "{{random::a::b}}"], choices, "mood:m2")
    assert set(choices) == {"mood:m2:{{random::a::b}}:0", "mood:m2:{{random::c::d}}:0", "mood:m2:{{random::a::b}}:1"}


def test_stored_random_pick_survives_inserted_macro():
    # Text-keyed (not position-keyed): a new macro inserted before an existing one cannot shift or steal its stored pick.
    choices: dict[str, str] = {}
    (before,) = resolve_stored_random(["{{random::kept::other}}"], choices, "mood:m2b")
    (after,) = resolve_stored_random(["{{random::new::stuff}} then {{random::kept::other}}"], choices, "mood:m2b")
    assert after.endswith(f"then {before}")


def test_stored_random_edited_options_reroll_fresh():
    # The key embeds the macro text, so editing the options orphans the old
    # pick and the edited macro rolls fresh from its own options.
    choices = {"mood:m3:{{random::removed::other}}:0": "removed"}
    (out,) = resolve_stored_random(["{{random::kept::other}}"], choices, "mood:m3")
    assert out in {"kept", "other"}
    assert choices["mood:m3:{{random::kept::other}}:0"] == out


def test_stored_random_leaves_roll_and_plain_text_alone():
    choices: dict[str, str] = {}
    assert resolve_stored_random(["plain {{roll::2d6}}", "", None], choices, "x") == ["plain {{roll::2d6}}", "", ""]
    assert choices == {}


# -- backtick literals (macros in `...` spans never resolve) --------------------


def test_backticked_macros_stay_literal_everywhere():
    text = "say `{{random::a::b}}` or `{{roll::2d6}}` to `{{user}}` and `{{char}}`"
    assert resolve_inline(text) == text
    assert resolve_message(text, "Alice", "Bot") == text
    choices: dict[str, str] = {}
    assert resolve_stored_random([text], choices, "mood:m") == [text]
    assert choices == {}


def test_backticked_literal_next_to_live_macro():
    out = resolve_message("use `{{random::a::b}}`, e.g. {{random::a}} for {{user}}", "Alice", "Bot")
    assert out == "use `{{random::a::b}}`, e.g. a for Alice"


def test_backtick_literal_is_idempotent_across_passes():
    once = resolve_message("keep `{{random::x::y}}` and {{random::only}}", "A", "B")
    assert once == "keep `{{random::x::y}}` and only"
    assert resolve_message(once, "A", "B") == once
    assert resolve_inline(once) == once


def test_has_inline_macros_ignores_backticked():
    assert not has_inline_macros("hi `{{random::a::b}}`")
    assert has_inline_macros("`{{random::a::b}}` and {{roll::1d6}}")


# -- {{pick}} alias and {{time}} ----------------------------------------------


def test_time_resolves_to_hh_mm():
    import re

    assert re.fullmatch(r"\d{2}:\d{2}", resolve_inline("{{time}}"))
    assert re.fullmatch(r"at \d{2}:\d{2}!", resolve_message("at {{TIME}}!", "U", "C", seed="conv-1"))


def test_date_resolves_to_iso_date():
    import re

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolve_inline("{{date}}"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolve_message("{{DATE}}", "U", "C", seed="conv-1"))
    assert has_inline_macros("today is {{date}}")


# -- {{// comment }} (the author-note macro) ----------------------------------


def test_macro_inside_comment_does_not_fire_in_messages():
    assert resolve_message("{{// pick {{random::a::b}} }}y", "U", "C") == "y"


def test_comment_with_unclosed_braces_stays_linear():
    # `{{//` followed by open braces and no closer used to re-scan to the end at
    # every position -- quadratic, and minutes on a field-sized input.
    blob = "{{//" + "{{" * 50_000
    start = time.monotonic()
    assert resolve_inline(blob) == blob
    assert time.monotonic() - start < 2.0


# -- {{trim}} (joins what the newlines around it separated) -------------------


def test_trim_registers_as_an_inline_macro_unless_backticked():
    assert has_inline_macros("a\n{{trim}}\nb")
    assert not has_inline_macros("write `{{trim}}` to join lines")


def test_trim_is_idempotent_and_resolves_in_messages():
    once = resolve_message("{{user}} said:\n{{trim}} hi {{char}}", "Alice", "Bot")
    assert once == "Alice said: hi Bot"
    assert resolve_message(once, "Alice", "Bot") == once
    assert Macros("Alice", "Bot", seed="conv-t").resolve_message("a\n{{trim}}\nb") == "ab"


# -- {{description}} (the one macro that substitutes prose, not a name) -------


def test_description_expands_into_text():
    m = Macros("Alice", "Bot", description="A tall woman with a limp.")
    assert m.resolve_message("Subject: {{description}}") == "Subject: A tall woman with a limp."


def test_description_is_case_insensitive():
    assert Macros("Alice", "Bot", description="prose").resolve_message("{{DESCRIPTION}} {{Description}}") == "prose prose"


def test_description_resolves_its_own_macros():
    # The ordering the feature rests on: descriptions are written with {{char}}/{{user}} in them, so the injection has to happen
    # before the name substitution rather than after it.
    m = Macros("Alice", "Bot", description="{{char}} distrusts {{user}}.")
    assert m.resolve_message("Sheet: {{description}}") == "Sheet: Bot distrusts Alice."


def test_description_resolves_cast_inside_itself():
    m = Macros("Alice", "Campfire", cast="Aria, Kael", description="Present: {{cast}}.")
    assert m.resolve_message("{{description}}") == "Present: Aria, Kael."


def test_description_inline_macros_fire_in_messages_not_prompts():
    # resolve_prompt exists so historical text does not re-roll; injected
    # description prose inherits that rule rather than escaping it.
    m = Macros("Alice", "Bot", description="a beast with {{roll::3d1}} limbs")
    assert m.resolve_message("{{description}}") == "a beast with 3 limbs"
    assert m.resolve_prompt("{{description}}") == "a beast with {{roll::3d1}} limbs"


def test_seeded_description_is_byte_stable():
    # The description lands in per-turn-rebuilt prompt fields, so a {{random}}
    # inside it must not re-roll and bust the shared KV prefix.
    m = Macros("Alice", "Bot", seed="conv-d", description="mood: {{random::calm::furious::wry}}")
    first = m.resolve_message("{{description}}")
    assert first == m.resolve_message("{{description}}")
    assert "{{random" not in first


def test_empty_description_leaves_the_macro_raw():
    # Same call {{cast}} makes in a solo chat: unresolved says "no value yet"
    # and survives to a later pass; blanking would delete the author's text.
    assert Macros("Alice", "Bot").resolve_message("x {{description}} y") == "x {{description}} y"
    assert Macros("Alice", "Bot", description="").resolve_prompt("{{description}}") == "{{description}}"


def test_self_reference_terminates_after_one_pass():
    m = Macros("Alice", "Bot", description="I am {{description}} incarnate.")
    assert m.resolve_message("{{description}}") == "I am  incarnate."


def test_description_containing_regex_template_syntax_is_literal():
    # A name never carries a backslash; a description does. Passed as a plain re.sub replacement these would be read as template
    # syntax -- \g<1> would raise and \n would become a newline.
    m = Macros("Alice", "Bot", description=r"writes \g<1> and \n on the wall")
    assert m.resolve_message("{{description}}") == r"writes \g<1> and \n on the wall"


def test_backticked_description_stays_literal():
    m = Macros("Alice", "Bot", description="prose")
    assert m.resolve_message("use `{{description}}` in a fragment") == "use `{{description}}` in a fragment"


def test_description_is_not_an_inline_macro():
    assert not has_inline_macros("{{description}}")


def test_from_settings_carries_the_description():
    m = Macros.from_settings({"user_name": "Alice"}, "Bot", description="prose")
    assert m.description == "prose"
    assert m.resolve_message("{{description}}") == "prose"


def test_card_description_is_the_field_not_the_persona_join():
    # char_context joins description + personality for the persona
    # block; the macro is named after the field, so it carries the field alone.
    card = {"description": "A tall woman with a limp.", "personality": "Wry, guarded"}
    assert card_description(card) == "A tall woman with a limp."


def test_card_description_handles_a_missing_card_or_field():
    assert card_description(None) == ""
    assert card_description({}) == ""
    assert card_description({"description": None}) == ""
    assert card_description({"description": "  padded  "}) == "padded"


# -- hostile values -----------------------------------------------------------


def test_names_are_inserted_literally_not_as_regex_templates():
    shrug = r"¯\_(ツ)_/¯"
    assert resolve_message("{{user}} and {{char}}", shrug, r"A\1\g<0>") == shrug + r" and A\1\g<0>"
    assert Macros(user="U", char="C", cast=r"B\2").resolve_message("{{cast}}") == r"B\2"


def test_every_name_can_be_a_random_option():
    macros = Macros(user="U", char="C", seed="s", cast="A, B")
    assert macros.resolve_message("{{random::{{cast}}::{{cast}}}}") == "A, B"
    assert macros.resolve_message("{{pick::{{user}}::{{user}}}}") == "U"
