"""Who this render is a picture *of*, in order.

Above `engine/` for the same stated reason as `references.py`: this reads
conversation state through the workflow toolkit, while the engine's half of the
split knows only bytes and slots.

**The one answer.** Two halves of a render need to agree on the cast -- the
reference image (whose likeness is sent) and the prompt (whose fixed appearance is
injected, and whose identity traits the composer must still spell out because no
picture of them was sent). Answering that question twice is how a likeness for Bob
rides along with a prompt that names only Alice. So both halves read this list, the
same posture `inference/group_context.py` has for card-field visibility.

Order is the contract:

* **Subject 0 is the primary** -- the card the route already resolved for this
  message (`_resolve_workflow_character`), which is what a solo chat has one of, what
  the `character` reference source means, and **the only subject a likeness is ever
  sent for**: a character has one reference image and a render sends one picture.
* **Subject 1..n are the rest of the cast that has spoken in this exchange so far**,
  in roster order. Whether each of them is *pictured* as well as described is the render
  target's to cap: a homogeneous cloud array carries one likeness per subject, a
  ComfyUI graph's structural inputs all take the primary's.

Scoping the tail to the *exchange* rather than the whole roster is deliberate: a
six-member scene where two people traded lines should not have four characters
described into a shot the scene analyzer then leaves them out of. An exchange is one
round -- the user's last message and every reply since -- which is what the picture is
of; see `_exchange_speakers` for why it is not `messages.beat_id`.

**"So far" is the whole of it.** The history handed in is already cut at the anchor
(`hooks._history_through`), and that cut is a stated invariant of this workflow, not
an accident: a render never composes from replies that came *after* the one being
visualized, and the regenerate ctx cannot even see them. So visualizing the first of
three replies in a round addresses one subject, and visualizing the last addresses
three. Widening this past the anchor would mean describing an action the analyzer
is not allowed to read -- a picture of something that has not happened yet.

Nothing here decides *visibility*. The analyzer decides who is in frame and
`composer._visible` drops the sheets of those it left out; this decides only who is a
candidate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..toolkit import CastMember, get_scene_cast, get_workflow_character_state
from .config import WORKFLOW_ID, normalize_profile


@dataclass(frozen=True)
class Subject:
    """One addressable person in this render.

    `card_id` is what a `character:` reference origin is keyed by, so a primary with
    no card is described in the prompt and never pictured -- a narrator member, or a
    group member whose card was deleted.

    `name` is what the *conversation* calls them: a group member's local display
    name, not the card's own, because the transcript the analyzer reads attributes
    replies by display name and the composer binds a subject to an analyzed cast
    entry by matching that name.
    """

    member_id: str
    card_id: str | None
    name: str
    profile: Mapping[str, Any] = field(default_factory=dict)


def _exchange_speakers(history: Sequence[Mapping[str, Any]], anchor_id: int) -> frozenset[str]:
    """The member ids that have spoken in the anchor's exchange, as a set.

    An **exchange** is one round: the user's most recent message and every reply that has
    followed it, up to and including the one being visualized. That is what the picture
    is of, and what this workflow has always documented.

    Deliberately *not* `messages.beat_id`. A beat is request-scoped -- one call of the
    group driver -- which matches a round only when the driver answers for everybody at
    once. Under `manual` turn mode the user gives one member the floor per click, so
    every reply is its own beat and the cast was permanently a party of one: a picture of
    two characters trading lines sent one likeness and described one person. The round is
    the honest unit, and it costs no query either -- `role` is on every history row.

    A *set*, not a sequence, because the caller orders the tail by the roster rather than
    by who spoke first, so the prompt's roster does not reshuffle when the same two
    people trade the first line between rounds. Answering with an order nothing reads
    would be an invitation to start reading it.

    A scene that opens with character greetings and no user message yet is one round, so
    everyone in it counts. The walk stops at the anchor rather than trusting the caller's
    cut, because a render never composes from replies that came after the one being
    visualized and that invariant is cheap to enforce twice.
    """
    speakers: list[str] = []
    for message in history:
        if message.get("role") == "user":
            # A new round begins; whoever spoke in the previous one is no longer in it.
            speakers.clear()
        else:
            member_id = message.get("speaker_member_id")
            if isinstance(member_id, str) and member_id:
                speakers.append(member_id)
        if message.get("id") == anchor_id:
            break
    return frozenset(speakers)


def _disambiguated(subjects: Sequence[Subject]) -> tuple[Subject, ...]:
    """The same subjects, with no two answering to the same name.

    A name is the only handle the model has on a subject: it is what the roster quotes,
    what `visible_subjects` comes back as, what names the subject the reference picture
    is of, and what binds an analyzed cast entry to a saved appearance sheet. `group_members.display_name` carries no uniqueness
    constraint -- only `speaker_key` and the active `character_card_id` do -- so two
    members really can both be "Guard", and then every one of those bindings is a
    coin flip: the single name comes back once and *both* sheets are injected.

    So a repeat is numbered off, and the model is handed something it can tell apart.
    Plain digits and a space: `(2)` would reach a booru encoder as attention syntax,
    and this text is written into image prompts.

    The first holder keeps the bare name, so the ordinary scene -- where nobody
    collides -- is untouched, and a rename never silently renumbers the others.
    """
    seen: dict[str, int] = {}
    out: list[Subject] = []
    for subject in subjects:
        key = subject.name.casefold()
        seen[key] = count = seen.get(key, 0) + 1
        out.append(subject if count == 1 or not subject.name else replace(subject, name=f"{subject.name} {count}"))
    return tuple(out)


async def _profile_for(card_id: str | None) -> dict:
    return normalize_profile(await get_workflow_character_state(card_id, WORKFLOW_ID) if card_id else None)


async def resolve(
    *,
    conversation_id: str,
    history: Sequence[Mapping[str, Any]],
    anchor_id: int,
    character_id: str | None,
    character: Mapping[str, Any] | None,
    profile: Mapping[str, Any],
) -> tuple[Subject, ...]:
    """The ordered subjects of one render.

    The primary is supplied rather than re-derived: the route resolved it before the
    hook ran (it is what the per-character profile lock and the stored profile are
    keyed by), and re-reading history for it here would be a second answer to a
    question already settled. What this adds is the tail.

    **The camera does not change who is in the scene.** First-person looks through the
    *user's* eyes, and the user is a persona rather than a cast member, so no subject is
    ever behind the lens -- every character in the exchange is in front of it. This used to
    truncate to the primary under first-person, which was a solo chat's arithmetic (one
    character, so one subject) applied to a group, and it silently dropped everyone else
    from both the picture and the prompt. Keeping the viewer out of frame is
    `prompts._SHOT_*_FIRST`'s job, and dropping people the analyzer invented is
    `composer._keep_subjects`'.

    A conversation with no resolvable primary (a narrator line in a group) has no
    subject 0, and therefore no subjects at all: the tail is *other* members relative
    to a primary, and there is no such thing as the second subject of a render that
    has no first.

    The answer leaves here with distinct names (`_disambiguated`), because every
    binding downstream of it is by name.
    """
    if not character_id:
        return ()
    cast = await get_scene_cast(conversation_id)
    by_id = {member.member_id: member for member in cast.members}
    primary = by_id.get(_anchor_member(history, anchor_id, character_id, by_id))
    subjects = [
        Subject(
            member_id=primary.member_id if primary else "",
            card_id=character_id,
            # The member's local name when the scene has one for them; a removed
            # member (still the anchor's speaker, no longer on the roster) and every
            # solo chat fall back to the card's.
            name=(primary.name if primary else "") or str((character or {}).get("name") or ""),
            profile=profile,
        )
    ]
    spoke = _exchange_speakers(history, anchor_id)
    for member in cast.members:
        if member.member_id in ("", subjects[0].member_id) or member.member_id not in spoke:
            continue
        # A narrator has no likeness and no appearance sheet; it speaks in the round
        # without ever being in the picture.
        if member.kind != "character" or not member.card_id:
            continue
        subjects.append(
            Subject(
                member_id=member.member_id,
                card_id=member.card_id,
                name=member.name,
                profile=await _profile_for(member.card_id),
            )
        )
    return _disambiguated(subjects)


def _anchor_member(
    history: Sequence[Mapping[str, Any]],
    anchor_id: int,
    character_id: str,
    by_id: Mapping[str, CastMember],
) -> str:
    """Which roster member the primary card is, when the scene still has one.

    Preferred by the anchor's own `speaker_member_id`, because two members may not
    share a card but a *tombstoned* one and an active one can: the route resolved the
    card from the anchor's speaker, so the anchor is the authority on which member
    that was. Falls back to the single active member holding that card, which is what
    a regenerate of a message written before speakers were recorded resolves to.
    """
    speaker = next((message.get("speaker_member_id") for message in history if message.get("id") == anchor_id), None)
    if isinstance(speaker, str) and speaker in by_id:
        return speaker
    matches = [member.member_id for member in by_id.values() if member.card_id == character_id]
    return matches[0] if len(matches) == 1 else ""
