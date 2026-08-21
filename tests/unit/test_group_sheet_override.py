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


def test_an_override_replaces_the_card_join_and_short_circuits_the_card_walk():
    """Not a merge and not an append: the sheet is one block of prose, and a
    scene that has cut the character's hair needs the old text gone, not
    contradicted two paragraphs later. The card is never consulted, so a member
    keeps its sheet after its card is deleted — and a cardless narrator, which
    has nothing to fall back to, can hold one at all."""
    assert _private_sheet(CARD) == "A scout of the Watch.\n\nPersonality: Terse."
    assert _private_sheet(CARD, "A scout, hair shorn, coat burned.") == "A scout, hair shorn, coat burned."
    assert _private_sheet({"description": "STALE", "personality": "STALE"}, "Current.") == "Current."
    assert _private_sheet(None, "The scene's voice.") == "The scene's voice."
    assert _private_sheet(None) == ""


def test_an_empty_override_blanks_rather_than_falling_back():
    """``if override`` would silently resurrect the card here. The user asked
    for no sheet; a scene that reinstates the card would be unfixable from the
    UI, since blank is the only way to say it."""
    assert _private_sheet(CARD, "") == ""
    assert _private_sheet(CARD, None) == _private_sheet(CARD)
