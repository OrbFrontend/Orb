"""Who this render is a picture *of*, in order.

Above `engine/` for the same stated reason as `references.py`: this reads
conversation state through the workflow toolkit, while the engine's half of the
split knows only bytes and slots.

**The one answer.** Two halves of a render need to agree on the cast -- the
reference slots (whose likeness goes in slot *n*) and the prompt (whose fixed
appearance is injected, and whose identity traits the composer must still spell
out because no picture of them was sent). Answering that question twice is how a
likeness for Bob rides along with a prompt that names only Alice. So both halves
read this list, the same posture `inference/group_context.py` has for card-field
visibility.

Order is the contract:

* **Subject 0 is the primary** -- the card the route already resolved for this
  message (`_resolve_workflow_character`), which is what a solo chat has one of and
  what the `character` reference source has always meant.
* **Subject 1..n are the rest of the cast that has spoken in this beat so far**, in
  roster order, which is what the `cast` reference source draws from.

Scoping the tail to the *beat* rather than the whole roster is deliberate: a
six-member scene where two people traded lines should not send four likenesses for
characters the scene analyzer then leaves out of the shot. `messages.beat_id` is
already on every history row the workflow ctx receives, so this costs no query.

**"So far" is the whole of it.** The history handed in is already cut at the anchor
(`hooks._history_through`), and that cut is a stated invariant of this workflow, not
an accident: a render never composes from replies that came *after* the one being
visualized, and the regenerate ctx cannot even see them. So visualizing the first of
three replies in a beat addresses one subject, and visualizing the last addresses
three. Widening this to the whole beat would mean sending a likeness for someone
whose reply the analyzer is not allowed to read -- a picture of an action that has
not happened yet.

Nothing here decides *visibility*. The analyzer decides who is in frame and
`composer.addressable_subjects` drops the tail members it left out, so a likeness is
never uploaded for someone the prompt does not name; this decides only who is a
candidate.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from ..toolkit import CastMember, get_scene_cast, get_workflow_character_state
from .config import WORKFLOW_ID, normalize_profile
from .pov import FIRST


@dataclass(frozen=True)
class Subject:
    """One addressable person in this render.

    `card_id` is what a `character:` reference origin is keyed by, so a subject with
    no card is addressable in the prompt and never in a slot -- a narrator member,
    or a group member whose card was deleted.

    `name` is what the *conversation* calls them: a group member's local display
    name, not the card's own, because the transcript the analyzer reads attributes
    replies by display name and the composer binds a subject to an analyzed cast
    entry by matching that name.
    """

    member_id: str
    card_id: str | None
    name: str
    profile: Mapping[str, Any] = field(default_factory=dict)


def _beat_speakers(history: Sequence[Mapping[str, Any]], anchor_id: int) -> frozenset[str]:
    """The member ids that have spoken in the anchor's beat, as a set.

    A beat is one round of the group driver: the user's message plus every reply it
    produced. `beat_id` is request-scoped and indexed, and `get_path_to_leaf` does
    `SELECT *`, so both it and `speaker_member_id` are already on the rows here.

    A *set*, not a sequence, because the caller orders the tail by the roster rather
    than by who spoke first -- `cast` ordinals must not move when the same two people
    trade the first line between beats. Answering with an order nothing reads would
    be an invitation to start reading it.

    An anchor with no `beat_id` -- a solo chat, or a group reply written before beats
    were recorded -- has no beat to widen to, and answers empty rather than falling
    back to the whole branch.
    """
    beat = next((message.get("beat_id") for message in history if message.get("id") == anchor_id), None)
    if not isinstance(beat, str) or not beat:
        return frozenset()
    return frozenset(
        member_id
        for message in history
        if message.get("beat_id") == beat
        for member_id in (message.get("speaker_member_id"),)
        if isinstance(member_id, str) and member_id
    )


def _disambiguated(subjects: Sequence[Subject]) -> tuple[Subject, ...]:
    """The same subjects, with no two answering to the same name.

    A name is the only handle the model has on a subject: it is what the roster quotes,
    what `visible_subjects` comes back as, and what binds an analyzed cast entry to a
    saved appearance sheet. `group_members.display_name` carries no uniqueness
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
    pov: str,
) -> tuple[Subject, ...]:
    """The ordered subjects of one render.

    The primary is supplied rather than re-derived: the route resolved it before the
    hook ran (it is what the per-character profile lock and the stored profile are
    keyed by), and re-reading history for it here would be a second answer to a
    question already settled. What this adds is the tail.

    *pov* truncates to one under first-person. That rule is not new -- the composer
    already dropped every non-owner from the analyzed cast on a first-person shot --
    but stating it here means the reference slots obey it too, instead of sending a
    likeness for someone the prompt is about to delete.

    A conversation with no resolvable primary (a narrator line in a group) has no
    subject 0, and therefore no subjects at all: `cast` addresses *other* members
    relative to a primary, and there is no such thing as the second subject of a
    render that has no first.

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
    if pov == FIRST:
        return _disambiguated(subjects)
    spoke = _beat_speakers(history, anchor_id)
    for member in cast.members:
        if member.member_id in ("", subjects[0].member_id) or member.member_id not in spoke:
            continue
        # A narrator has no likeness and no appearance sheet; it speaks in the beat
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
