"""``card_sheet_override`` — the scene-local sheet a member reads about itself.

The counterpart to ``public_profile_override``: that one is what the rest of the
cast sees, this one is what the member reads about *itself*. Both resolve on
``is not None`` rather than truthiness, so a deliberate blanking stays
distinguishable from an absent override — the assertion this file exists for,
because the two are one character apart in the source and identical in every
test that only ever passes ``None``.
"""

from __future__ import annotations

from backend.database.queries.group_members import _private_sheet

CARD = {"description": "A scout of the Watch.", "personality": "Terse."}


def test_an_absent_override_renders_the_card_join_byte_for_byte():
    """``NULL`` is what every row that predates the column carries, so this is
    the no-change case: today's behaviour, unchanged."""
    assert _private_sheet(CARD) == "A scout of the Watch.\n\nPersonality: Terse."
    assert _private_sheet(CARD, None) == _private_sheet(CARD)


def test_a_stored_override_replaces_the_card_join_entirely():
    """Not a merge and not an append: the sheet is one block of prose, and a
    scene that has cut the character's hair needs the old text gone, not
    contradicted two paragraphs later."""
    assert _private_sheet(CARD, "A scout, hair shorn, coat burned.") == "A scout, hair shorn, coat burned."


def test_an_empty_override_blanks_rather_than_falling_back():
    """``if override`` would silently resurrect the card here. The user asked
    for no sheet; a scene that reinstates the card would be unfixable from the
    UI, since blank is the only way to say it."""
    assert _private_sheet(CARD, "") == ""


def test_a_cardless_member_still_gets_its_override():
    """A narrator has no card to fall back to, which is a reason to let it hold
    a sheet, not a reason to refuse one."""
    assert _private_sheet(None, "The scene's voice.") == "The scene's voice."
    assert _private_sheet(None) == ""


def test_the_card_is_never_consulted_when_an_override_is_present():
    """The override short-circuits before the card walk, so a member can keep a
    sheet after its card is deleted."""
    assert _private_sheet({"description": "STALE", "personality": "STALE"}, "Current.") == "Current."
