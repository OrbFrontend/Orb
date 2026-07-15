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


# ── resolve_stored_random (per-conversation choice map) ──────────────────────


def test_stored_random_records_and_reuses():
    choices: dict[str, str] = {}
    (first,) = resolve_stored_random(["{{random::crimson::azure}}"], choices, "mood:m1")
    assert choices == {"mood:m1:0": first}
    (again,) = resolve_stored_random(["{{random::crimson::azure}}"], dict(choices), "mood:m1")
    assert again == first


def test_stored_random_shared_counter_across_texts():
    choices: dict[str, str] = {}
    resolve_stored_random(["{{random::a::b}} {{random::c::d}}", "{{random::e::f}}"], choices, "mood:m2")
    assert set(choices) == {"mood:m2:0", "mood:m2:1", "mood:m2:2"}


def test_stored_random_rerolls_when_choice_no_longer_an_option():
    choices = {"mood:m3:0": "removed"}
    (out,) = resolve_stored_random(["{{random::kept::other}}"], choices, "mood:m3")
    assert out in {"kept", "other"}
    assert choices["mood:m3:0"] == out


def test_stored_random_leaves_roll_and_plain_text_alone():
    choices: dict[str, str] = {}
    out = resolve_stored_random(["plain {{roll::2d6}}", "", None], choices, "x")  # type: ignore[list-item]
    assert out == ["plain {{roll::2d6}}", "", ""]
    assert choices == {}


def test_stored_random_reuses_even_with_fresh_option_order():
    # Membership, not position, decides reuse: reordering options keeps the pick.
    choices = {"interactive:f1:0": "beta"}
    (out,) = resolve_stored_random(["{{random::beta::alpha}}"], choices, "interactive:f1")
    assert out == "beta"
