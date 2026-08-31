"""Resolve the ordered subject candidates for an image render."""

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


def _round_speakers(history: Sequence[Mapping[str, Any]], anchor_id: int) -> frozenset[str]:
    """Return member ids that spoke in the anchored round."""
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
    """Disambiguate subject names."""
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
    """Resolve the ordered subjects of one render."""
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
    spoke = _round_speakers(history, anchor_id)
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
