"""Unit tests for the inline macro engine (backend/core/macros.py).

Covers the {{random::a::b}} grammar, the fresh-roll persist-boundary entry
(resolve_inline), the per-conversation choice map (resolve_stored_random), the
seeded Macros determinism used for per-turn-rebuilt prompt fields, and the
idempotency invariant the persist boundary relies on (resolving already
resolved text is a no-op).
"""

from __future__ import annotations

from backend.core.macros import (
    Macros,
    has_inline_macros,
    resolve_inline,
    resolve_message,
    resolve_stored_random,
)

# ── grammar / resolve_inline ─────────────────────────────────────────────────


def test_random_two_options_picks_a_member():
    assert resolve_inline("{{random::red::blue}}") in {"red", "blue"}


def test_random_single_option_is_deterministic():
    assert resolve_inline("go {{random::north}}") == "go north"


def test_random_empty_options_resolve_to_empty_string():
    assert resolve_inline("{{random::}}") == ""
    assert resolve_inline("x{{random::a::}}y") in {"xay", "xy"}


def test_random_case_insensitive():
    assert resolve_inline("{{RANDOM::up}}") == "up"
    assert resolve_inline("{{Random::up}}") == "up"


def test_random_multiline_options():
    assert resolve_inline("{{random::line1\nline2}}") == "line1\nline2"


def test_random_non_greedy_terminates_at_first_close():
    # Two macros on one line must not merge into one greedy match.
    assert resolve_inline("{{random::a}} and {{random::b}}") == "a and b"
    assert resolve_inline("{{random::a}}{{random::b}}") == "ab"


def test_roll_still_fires_and_random_leaves_user_char_alone():
    out = resolve_inline("{{roll::2d1}} {{random::x}} {{user}} {{char}}")
    assert out == "2 x {{user}} {{char}}"


def test_resolve_inline_handles_empty_and_none():
    assert resolve_inline("") == ""
    assert resolve_inline(None) == ""  # type: ignore[arg-type]


# ── has_inline_macros ────────────────────────────────────────────────────────


def test_has_inline_macros():
    assert has_inline_macros("hi {{random::a::b}}")
    assert has_inline_macros("hi {{roll::2d6}}")
    assert not has_inline_macros("hi {{user}}, meet {{char}}")
    assert not has_inline_macros("plain text")
    assert not has_inline_macros("")


# ── idempotency (the persist-boundary invariant) ─────────────────────────────


def test_resolve_message_idempotent_on_resolved_text():
    text = "{{user}} rolls {{roll::3d1}} and picks {{random::only}} for {{char}}"
    once = resolve_message(text, "Alice", "Bot")
    assert once == "Alice rolls 3 and picks only for Bot"
    assert resolve_message(once, "Alice", "Bot") == once


def test_resolve_inline_idempotent():
    once = resolve_inline("{{random::alpha::beta}} / {{roll::1d1}}")
    assert resolve_inline(once) == once


# ── seeded determinism (per-turn-rebuilt prompt fields) ──────────────────────


def test_seeded_macros_are_deterministic():
    m = Macros("Alice", "Bot", seed="conv-1")
    text = "sky is {{random::red::green::blue}} and sea is {{random::red::green::blue}}"
    first = m.resolve_message(text)
    assert first == m.resolve_message(text)
    assert "{{random" not in first


def test_seeded_pick_survives_surrounding_edits():
    # The ordinal keys on the macro's own text, so unrelated prose changes
    # around it must not re-roll the pick.
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
    counts = {Macros("A", "B", seed=f"conv-{i}").resolve_message("{{roll::10d100}}") for i in range(20)}
    assert len(counts) > 1


def test_unseeded_roll_rolls_fresh():
    # 20 draws of 10d100 all landing on the same total would mean the RNG froze.
    assert len({resolve_inline("{{roll::10d100}}") for _ in range(20)}) > 1


# ── resolve_stored_random (per-conversation choice map) ──────────────────────


def test_stored_random_records_and_reuses():
    choices: dict[str, str] = {}
    (first,) = resolve_stored_random(["{{random::crimson::azure}}"], choices, "mood:m1")
    assert choices == {"mood:m1:{{random::crimson::azure}}:0": first}
    (again,) = resolve_stored_random(["{{random::crimson::azure}}"], dict(choices), "mood:m1")
    assert again == first


def test_stored_random_keys_by_macro_text_and_ordinal():
    # Distinct macros key independently; a repeat of the same macro gets its
    # own ordinal; keys span all texts of the call.
    choices: dict[str, str] = {}
    resolve_stored_random(["{{random::a::b}} {{random::c::d}}", "{{random::a::b}}"], choices, "mood:m2")
    assert set(choices) == {
        "mood:m2:{{random::a::b}}:0",
        "mood:m2:{{random::c::d}}:0",
        "mood:m2:{{random::a::b}}:1",
    }


def test_stored_random_pick_survives_inserted_macro():
    # Text-keyed (not position-keyed): a new macro inserted before an existing
    # one cannot shift or steal its stored pick.
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
    out = resolve_stored_random(["plain {{roll::2d6}}", "", None], choices, "x")  # type: ignore[list-item]
    assert out == ["plain {{roll::2d6}}", "", ""]
    assert choices == {}


# ── backtick literals (macros in `…` spans never resolve) ────────────────────


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
    text = "keep `{{random::x::y}}` and {{random::only}}"
    once = resolve_message(text, "A", "B")
    assert once == "keep `{{random::x::y}}` and only"
    assert resolve_message(once, "A", "B") == once
    assert resolve_inline(once) == once


def test_has_inline_macros_ignores_backticked():
    assert not has_inline_macros("hi `{{random::a::b}}`")
    assert has_inline_macros("`{{random::a::b}}` and {{roll::1d6}}")


def test_unpaired_or_multiline_backticks_do_not_escape():
    # A lone backtick opens no span; spans don't cross newlines.
    assert resolve_inline("` {{random::a}}") == "` a"
    assert resolve_inline("`no close\n{{random::a}}`") == "`no close\na`"


# ── {{pick}} alias and {{time}} ──────────────────────────────────────────────


def test_pick_is_random_alias():
    assert resolve_inline("{{pick::red::blue}}") in {"red", "blue"}
    assert resolve_inline("{{PICK::up}}") == "up"


def test_pick_seeded_deterministic():
    text = "{{pick::a::b::c::d::e::f}}"
    first = resolve_message(text, "U", "C", seed="conv-1")
    assert all(resolve_message(text, "U", "C", seed="conv-1") == first for _ in range(5))


def test_pick_works_in_stored_random():
    choices: dict[str, str] = {}
    (out,) = resolve_stored_random(["{{pick::x::y}}"], choices, "mood:m")
    assert out in {"x", "y"}
    assert resolve_stored_random(["{{pick::x::y}}"], choices, "mood:m") == [out]


def test_time_resolves_to_hh_mm():
    import re

    assert re.fullmatch(r"\d{2}:\d{2}", resolve_inline("{{time}}"))
    assert re.fullmatch(r"at \d{2}:\d{2}!", resolve_message("at {{TIME}}!", "U", "C", seed="conv-1"))


def test_time_detected_and_backtick_literal():
    assert has_inline_macros("it is {{time}}")
    assert has_inline_macros("it is {{pick::a::b}}")
    assert not has_inline_macros("say `{{time}}`")
    assert resolve_inline("say `{{time}}`") == "say `{{time}}`"


def test_date_resolves_to_iso_date():
    import re

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolve_inline("{{date}}"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", resolve_message("{{DATE}}", "U", "C", seed="conv-1"))
    assert has_inline_macros("today is {{date}}")
    assert resolve_inline("say `{{date}}`") == "say `{{date}}`"
