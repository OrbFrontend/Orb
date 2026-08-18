"""The public-profile join: one renderer, one key set, one meaning for ``""``.

``render_public_profile`` lives beside ``set_public_profile`` (its only writer)
in ``database/queries/character_cards.py``, and the group cast projection
delegates to it. These pins exist because the two halves failing to agree on the
key set is silent: a field would be stored and never rendered, or rendered from
a key nothing writes.
"""

from __future__ import annotations

import pytest

from backend.database.queries.character_cards import render_public_profile
from backend.database.queries.group_members import _public_profile

CARD = {"extensions": {"orb": {"public_profile": {"appearance": "Tall, in road-worn green.", "role": "Wandering bard."}}}}


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"appearance": "Tall.", "role": "Bard."}, "Appearance: Tall.\nRole: Bard."),
        ({"appearance": "Tall."}, "Appearance: Tall."),
        ({"role": "Bard."}, "Role: Bard."),
        # Field order is the render order, not the dict's.
        ({"role": "Bard.", "appearance": "Tall."}, "Appearance: Tall.\nRole: Bard."),
        ({"appearance": "  Tall.  ", "role": "\tBard.\n"}, "Appearance: Tall.\nRole: Bard."),
        # Blank, absent and wrong-typed fields are all "nothing to say".
        ({"appearance": "   ", "role": ""}, ""),
        ({"appearance": None, "role": 7}, ""),
        ({}, ""),
        (None, ""),
        ("Appearance: Tall.", ""),
        ([("appearance", "Tall.")], ""),
    ],
)
def test_render_public_profile_shapes(profile, expected):
    assert render_public_profile(profile) == expected


def test_the_cast_projection_and_the_renderer_are_the_same_definition():
    """A card-derived member profile is exactly what the renderer produces —
    so an LLM-drafted scene override written in that shape reads identically in
    the assembled prompt to a member who has no override at all."""
    assert _public_profile(CARD, None) == render_public_profile(CARD["extensions"]["orb"]["public_profile"])
    assert _public_profile(CARD, None) == "Appearance: Tall, in road-worn green.\nRole: Wandering bard."


def test_a_cardless_or_profileless_member_projects_to_nothing():
    assert _public_profile(None, None) == ""
    assert _public_profile({}, None) == ""
    assert _public_profile({"extensions": {}}, None) == ""
    assert _public_profile({"extensions": {"orb": {}}}, None) == ""
    assert _public_profile({"extensions": "not-a-dict"}, None) == ""


def test_an_empty_override_blanks_the_card_profile_rather_than_falling_back():
    """`override is not None`, not `if override`. An empty string is the user
    deliberately publishing nothing about this member in this scene; only an
    absent override (`None`) falls back to the card. The client coerces a
    whitespace-only box to null at save, so a stored `""` is always explicit."""
    assert _public_profile(CARD, "") == ""
    assert _public_profile(CARD, "typed") == "typed"
    assert _public_profile(CARD, None) != ""
