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


@pytest.mark.asyncio
async def test_the_tail_stops_at_the_anchor_not_at_the_end_of_the_beat(_scene):
    """Scoped to the beat *so far*, because that is all the render may read.

    `hooks._history_through` cuts the branch at the message being visualized, and that
    cut is deliberate: a render never composes from replies that came after it, and the
    regenerate ctx cannot even see them. So the first of three replies addresses one
    subject and the last addresses three -- and the picker's plain `cast` row is
    documented as the strict choice for exactly this reason.
    """
    _scene([_member("m1", "Iris", "card-a"), _member("m2", "Ashley", "card-b")])
    # The whole beat, as it sits in the database: Iris answered, then Ashley.
    beat = [_msg(1, speaker="m1", beat="b"), _msg(2, speaker="m2", beat="b")]

    # Visualizing Ashley's reply -- the last -- sees both.
    assert [s.name for s in await _resolve(history=beat, anchor_id=2)] == ["Ashley", "Iris"]
    # Visualizing Iris's reply sees only her: Ashley had not spoken yet, and the
    # history the hook hands in is already cut there.
    assert [s.name for s in await _resolve(history=beat[:1], anchor_id=1)] == ["Iris"]


@pytest.mark.asyncio
async def test_two_members_with_one_name_are_told_apart(_scene):
    """`group_members.display_name` carries no uniqueness constraint -- only
    `speaker_key` and the active `character_card_id` do -- so two members really can
    both be "Guard".

    Every binding downstream is by name: the roster quotes it, `visible_subjects` comes
    back as it, and the composer matches an analyzed cast entry on it. Left alone, one
    "Guard" in the answer would inject *both* sheets and name one person for two
    images. Numbered off with plain digits, never `(2)`, which a booru encoder reads as
    attention syntax in a prompt this text is written into.
    """
    _scene(
        [_member("m1", "Guard", "card-a"), _member("m2", "Guard", "card-b")],
        {"card-b": {"appearance_prompt": "scarred jaw"}},
    )
    history = [_msg(1, speaker="m2", beat="b"), _msg(2, speaker="m1", beat="b")]

    resolved = await _resolve(history=history, anchor_id=2)

    assert [(s.card_id, s.name) for s in resolved] == [("card-a", "Guard"), ("card-b", "Guard 2")]
    # The first holder keeps the bare name, so an ordinary scene is untouched.
    _scene([_member("m1", "Iris", "card-a"), _member("m2", "Ashley", "card-b")])
    assert [s.name for s in await _resolve(history=history, anchor_id=2)] == ["Iris", "Ashley"]
