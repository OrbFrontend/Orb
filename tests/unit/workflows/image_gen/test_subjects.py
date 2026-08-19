"""Who a render is a picture of, and in what order.

The order is not cosmetic: it is what decides which member's likeness goes into
which reference slot and whose fixed appearance the prompt injects. Getting it
wrong is silent -- a perfectly good picture of the wrong person -- so the rules are
pinned here rather than left to the end-to-end path.
"""

from __future__ import annotations

import pytest

from backend.core.domain_types import CastMember, TurnCast
from backend.workflows.image_gen import subjects as subjects_mod
from backend.workflows.image_gen.pov import FIRST, THIRD


def _member(member_id: str, name: str, card_id: str | None = None, kind: str = "character") -> CastMember:
    return CastMember(
        member_id=member_id,
        speaker_key=member_id,
        card_id=card_id if card_id is not None else f"card-{member_id}",
        name=name,
        kind=kind,
        public_profile="",
        private_sheet="",
        mes_example="",
        post_history="",
    )


def _msg(msg_id: int, *, speaker: str | None = None, beat: str | None = None) -> dict:
    return {"id": msg_id, "speaker_member_id": speaker, "beat_id": beat}


@pytest.fixture
def _scene(monkeypatch):
    """Install a roster and the per-card image_gen profiles behind the toolkit."""

    def install(members, profiles=None):
        async def cast(_cid):
            return TurnCast(bool(members and members[0].member_id), tuple(members))

        async def state(card_id, _wid):
            return (profiles or {}).get(card_id)

        monkeypatch.setattr(subjects_mod, "get_scene_cast", cast)
        monkeypatch.setattr(subjects_mod, "get_workflow_character_state", state)

    return install


async def _resolve(**kwargs):
    base = {
        "conversation_id": "c1",
        "history": [],
        "anchor_id": 2,
        "character_id": "card-a",
        "character": {"name": "Card Name"},
        "profile": {"appearance_prompt": "silver hair"},
        "pov": THIRD,
    }
    return await subjects_mod.resolve(**{**base, **kwargs})


@pytest.mark.asyncio
async def test_a_solo_chat_has_exactly_one_subject(_scene):
    """The synthesized solo member carries no id, so nothing may be read off it as a
    roster position -- and the card's own name is what names the subject."""
    _scene([_member("", "Iris", card_id="card-a")])

    resolved = await _resolve(history=[_msg(2)])

    assert [(s.card_id, s.name) for s in resolved] == [("card-a", "Iris")]
    assert resolved[0].profile["appearance_prompt"] == "silver hair"


@pytest.mark.asyncio
async def test_no_primary_means_no_subjects(_scene):
    """A narrator line resolves no card, and `cast` addresses members *relative to* a
    primary -- there is no second subject of a render that has no first."""
    _scene([_member("m1", "Iris"), _member("m2", "Ashley")])

    assert await _resolve(character_id=None) == ()


@pytest.mark.asyncio
async def test_the_tail_is_the_beat_and_nothing_wider(_scene):
    """Roster order, scoped to who actually spoke in this beat.

    The bound matters: sending a likeness for every member of a six-person scene
    would hand the image model five people the analyzer is about to leave out.
    """
    _scene(
        [_member("m1", "Iris", "card-a"), _member("m2", "Ashley", "card-b"), _member("m3", "Ren", "card-c")],
        {"card-b": {"appearance_prompt": "red coat"}},
    )
    history = [
        # An earlier beat: Ren spoke, and is not part of this picture.
        _msg(1, speaker="m3", beat="beat-0"),
        _msg(2, speaker="m2", beat="beat-1"),
        _msg(3, speaker="m1", beat="beat-1"),
    ]

    resolved = await _resolve(history=history, anchor_id=3)

    # Iris is the anchor's speaker and leads; Ashley follows in roster order.
    assert [(s.member_id, s.name) for s in resolved] == [("m1", "Iris"), ("m2", "Ashley")]
    assert resolved[1].profile["appearance_prompt"] == "red coat"


@pytest.mark.asyncio
async def test_an_anchor_with_no_beat_has_no_tail(_scene):
    """A reply written before beats were recorded has no beat to widen to, and must
    not fall back to the whole branch."""
    _scene([_member("m1", "Iris", "card-a"), _member("m2", "Ashley", "card-b")])
    history = [_msg(1, speaker="m2"), _msg(2, speaker="m1")]

    assert [s.name for s in await _resolve(history=history)] == ["Iris"]


@pytest.mark.asyncio
async def test_first_person_truncates_to_the_one_subject(_scene):
    """Stated once, here, rather than again in the composer: a first-person shot looks
    at one person, so no slot may be handed a likeness the prompt is about to drop."""
    _scene([_member("m1", "Iris", "card-a"), _member("m2", "Ashley", "card-b")])
    history = [_msg(1, speaker="m2", beat="b"), _msg(2, speaker="m1", beat="b")]

    assert [s.name for s in await _resolve(history=history, pov=FIRST)] == ["Iris"]
    assert [s.name for s in await _resolve(history=history, pov=THIRD)] == ["Iris", "Ashley"]


@pytest.mark.asyncio
async def test_a_narrator_in_the_beat_is_never_a_subject(_scene):
    """It speaks without being in the picture: no card, so no likeness and no sheet."""
    _scene([_member("m1", "Iris", "card-a"), _member("m2", "Narrator", card_id=None, kind="narrator")])
    history = [_msg(1, speaker="m2", beat="b"), _msg(2, speaker="m1", beat="b")]

    assert [s.name for s in await _resolve(history=history)] == ["Iris"]


@pytest.mark.asyncio
async def test_a_removed_speaker_still_leads_under_the_card_name(_scene):
    """The anchor's speaker was tombstoned since. The route still resolved their card,
    so the render is still of them -- named by the card, which is all that is left."""
    _scene([_member("m2", "Ashley", "card-b")])
    history = [_msg(2, speaker="m-gone", beat="b")]

    resolved = await _resolve(history=history)

    assert [(s.member_id, s.card_id, s.name) for s in resolved] == [("", "card-a", "Card Name")]
