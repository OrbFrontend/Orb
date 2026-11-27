"""Build the character context for group conversations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from ..core import CastMember, GroupContextMode, Macros, TurnCast, fill_original
from .layout import SectionInputs, render_card_template

CAST_HEADING = "## Cast"


def carries_public_cast(mode: GroupContextMode) -> bool:
    """Whether the shared body needs public profiles: private/swap do, shared does not.

    Private card text stays owner-only; swap appends the active card after the profiles. Shared mode already exposes every card.
    """
    return mode != "shared"


def prefix_is_speaker_scoped(mode: GroupContextMode) -> bool:
    """True when the shared system body depends on *who* is speaking.

    Only Classic card swap substitutes an active card before history, so only it needs one frozen prefix per selected speaker --
    plus a neutral one for the Director and the pre-pipeline hooks, which both run before a speaking plan exists.
    """
    return mode == "swap"


def prefix_cache_shape(mode: GroupContextMode, speaker: CastMember | None) -> str:
    """The tracker lane shape for a speaker's prefix: one per speaker under swap, the shared lane otherwise."""
    return f"swap:{speaker.speaker_key}" if speaker is not None and prefix_is_speaker_scoped(mode) else ""


def tail_carries_identity(mode: GroupContextMode) -> bool:
    """True when the speaker's description/personality/examples ride the trailing Writer message rather than the shared system
    body.

    A *card's* ``post_history_instructions`` is not covered by this: it is active-only in every mode and always stays in the
    tail. The scene's own directive is not a member field at all and rides the shared body.
    """
    return mode == "private"


def roster_names(cast: TurnCast) -> str:
    return ", ".join(member.name for member in cast.members)


def macro_identity(conv: Mapping[str, Any], cast: TurnCast) -> tuple[str, str]:
    """Return identity macros for one conversation."""
    if not cast.grouped:
        return str(conv.get("character_name") or ""), ""
    return str(conv.get("title") or ""), roster_names(cast)


def member_macros(macros: Macros | None, member: CastMember, roster: str) -> Macros | None:
    """Scope char, description and personality macros to a member.

    Preserve user and seed for byte-stable resolution. A scene sheet override stands in for the description and empties the
    personality. The member's fields resolve once, at the member's own places.
    """
    if macros is None:
        return None
    return macros._replace(char=member.name, cast=roster).with_card_fields(
        member.description, member.personality, member_place(member, "")
    )


def member_place(member: CastMember, field: str) -> str:
    """Where one of a member's texts rolls: the same place wherever that text renders."""
    return f"member:{member.member_id}:{field}"


def _resolver(macros: Macros | None, place: str):
    return macros.at(place).resolve_message if macros else (lambda text: text)


def _render_public_cast(cast: TurnCast, macros: Macros | None, roster: str) -> str:
    """Render confirmed public profiles for the cast."""
    scoped = macros._replace(cast=roster) if macros else None
    parts = [f"\n\n{CAST_HEADING}"]
    for member in cast.members:
        parts.append(f"\n### {member.name}")
        if member.public_profile:
            parts.append(f"\n{_resolver(scoped, member_place(member, 'public_profile'))(member.public_profile)}")
    return "".join(parts)


def render_member_card(member: CastMember, macros: Macros | None, roster: str, template: str, sections: SectionInputs) -> str:
    """One member's card from the Group cast *template*: the card placeholders stand for the member's own fields, the chat's
    sections keep their meaning. Empty while every card section the template names is empty."""
    scoped = member_macros(macros, member, roster)
    examples = _resolver(scoped, member_place(member, "mes_example"))(member.mes_example) if member.mes_example else ""
    card = replace(
        sections,
        description=scoped.description if scoped else member.description,
        personality=scoped.personality if scoped else member.personality,
        character_heading=False,
        examples=examples,
    )
    return render_card_template(template, card, _resolver(scoped, member_place(member, "cast")))


def render_cast_section(cast: TurnCast, macros: Macros | None, template: str, sections: SectionInputs) -> str:
    """The whole group identity section of the system body, leading newlines included; each member card from *template*.

    Returns ``""`` for a solo turn. In Classic card swap a *cast* with no ``speaker`` renders the neutral base every speaker's
    prefix shares: the public cast and nothing after it.
    """
    if not cast.grouped:
        return ""
    roster = roster_names(cast)
    if not carries_public_cast(cast.context_mode):
        # Shared keeps a names-only list ahead of the cards: it guarantees every active member is named even when its card is
        # empty. The other two get that floor from the public cast's per-member headings.
        parts = [f"\n\n{CAST_HEADING}\n{roster}"]
        cards = (render_member_card(member, macros, roster, template, sections) for member in cast.members)
        parts.extend(f"\n\n{card}" for card in cards if card)
        return "".join(parts)
    parts = [_render_public_cast(cast, macros, roster)]
    # Swap alone appends a card, and only once a speaker is chosen -- the Director and the pre-pipeline hooks run before that
    # and stop at the public cast, which is exactly the base each speaker then extends.
    if prefix_is_speaker_scoped(cast.context_mode) and cast.speaker is not None:
        if card := render_member_card(cast.speaker, macros, roster, template, sections):
            parts.append(f"\n\n{card}")
    return "".join(parts)


def speaker_instructions(member: CastMember, *, prevent_prompt_overrides: bool) -> str:
    """The member card's post-history instructions as the trailing Writer message carries them; empty when overrides are off."""
    return "" if prevent_prompt_overrides else fill_original(member.post_history, "")


def largest_cast_section(cast: TurnCast, macros: Macros | None, template: str, sections: SectionInputs) -> str:
    """The cast section of the largest prefix any speaker gets: under Classic card swap, the public cast plus the largest
    unmuted speaker's card; the shared section otherwise."""
    neutral = render_cast_section(cast._replace(speaker=None), macros, template, sections)
    if not prefix_is_speaker_scoped(cast.context_mode):
        return neutral
    speakers = [
        render_cast_section(cast._replace(speaker=member), macros, template, sections)
        for member in cast.members
        if not member.muted
    ]
    return max([neutral, *speakers], key=len)
