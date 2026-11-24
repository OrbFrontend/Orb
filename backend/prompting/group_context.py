"""Build the character context for group conversations."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..core import CastMember, GroupContextMode, Macros, TurnCast, fill_original

CAST_HEADING = "## Cast"
DOSSIER_HEADING = "## Character dossier: "
ACTIVE_CARD_HEADING = "## Character: "
EXAMPLE_HEADING = "## Example Dialogue"
# A dossier is a `##` section, so its examples nest one level deeper or they would close the dossier they belong to.
DOSSIER_EXAMPLE_HEADING = "### Example Dialogue"


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
    personality.
    """
    if macros is None:
        return None
    return macros._replace(char=member.name, cast=roster, description=member.description, personality=member.personality)


def _resolver(macros: Macros | None):
    return macros.resolve_message if macros else (lambda text: text)


def _examples_block(text: str, heading: str) -> str:
    """Render example dialogue under *heading*, honouring the V2 ``<START>`` marker."""
    return text.replace("<START>", heading) if "<START>" in text else f"{heading}\n{text}"


def _render_public_cast(cast: TurnCast, macros: Macros | None, roster: str) -> str:
    """Render confirmed public profiles for the cast."""
    resolve = _resolver(macros._replace(cast=roster) if macros else None)
    parts = [f"\n\n{CAST_HEADING}"]
    for member in cast.members:
        parts.append(f"\n### {member.name}")
        if member.public_profile:
            parts.append(f"\n{resolve(member.public_profile)}")
    return "".join(parts)


def render_dossier(member: CastMember, macros: Macros | None, roster: str) -> str:
    """Render one member's identity dossier."""
    resolve = _resolver(member_macros(macros, member, roster))
    body = []
    if member.private_sheet:
        body.append(resolve(member.private_sheet))
    if member.mes_example:
        body.append(_examples_block(resolve(member.mes_example), DOSSIER_EXAMPLE_HEADING))
    if not body:
        return ""
    return f"\n\n{DOSSIER_HEADING}{member.name}\n" + "\n\n".join(body)


def _render_active_card(speaker: CastMember, macros: Macros | None, roster: str) -> str:
    """Classic card swap: the selected member's identity fields, card-style.

    Appended after the public cast, so the speaker is described twice on purpose: the short public line the rest of the cast
    reads about it, then its own card. The same doubling exists under Private, where the card lands in the tail instead --
    neither mode suppresses a member's public profile just because that member happens to be speaking.
    """
    resolve = _resolver(member_macros(macros, speaker, roster))
    parts = [f"\n\n{ACTIVE_CARD_HEADING}{speaker.name}"]
    if speaker.private_sheet:
        parts.append(f"\n{resolve(speaker.private_sheet)}")
    if speaker.mes_example:
        parts.append("\n\n" + _examples_block(resolve(speaker.mes_example), EXAMPLE_HEADING))
    return "".join(parts)


def render_cast_section(cast: TurnCast, macros: Macros | None) -> str:
    """The whole group identity section of the system body, leading newlines included.

    Returns ``""`` for a solo turn. In Classic card swap a *cast* with no ``speaker`` renders the neutral base every speaker's
    prefix shares: the public cast and nothing after it.
    """
    if not cast.grouped:
        return ""
    roster = roster_names(cast)
    if not carries_public_cast(cast.context_mode):
        # Shared keeps a names-only list ahead of the dossiers: it guarantees every active member is named even when its dossier
        # is empty. The other two get that floor from the public cast's per-member headings.
        parts = [f"\n\n{CAST_HEADING}\n{roster}"]
        parts.extend(render_dossier(member, macros, roster) for member in cast.members)
        return "".join(parts)
    parts = [_render_public_cast(cast, macros, roster)]
    # Swap alone appends a card, and only once a speaker is chosen -- the Director and the pre-pipeline hooks run before that
    # and stop at the public cast, which is exactly the base each speaker then extends.
    if prefix_is_speaker_scoped(cast.context_mode) and cast.speaker is not None:
        parts.append(_render_active_card(cast.speaker, macros, roster))
    return "".join(parts)


def speaker_instructions(member: CastMember, *, prevent_prompt_overrides: bool) -> str:
    """The member card's post-history instructions as the trailing Writer message carries them; empty when overrides are off."""
    return "" if prevent_prompt_overrides else fill_original(member.post_history, "")


def largest_cast_section(cast: TurnCast, macros: Macros | None) -> str:
    """The cast section of the largest prefix any speaker gets: under Classic card swap, the public cast plus the largest
    unmuted speaker's card; the shared section otherwise."""
    neutral = render_cast_section(cast._replace(speaker=None), macros)
    if not prefix_is_speaker_scoped(cast.context_mode):
        return neutral
    speakers = [render_cast_section(cast._replace(speaker=member), macros) for member in cast.members if not member.muted]
    return max([neutral, *speakers], key=len)
