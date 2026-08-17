from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast as typed_cast

from .. import database as db
from ..core import CastMember, GroupContextMode, TurnCast


def _public_profile(card: Mapping | None, override: str | None) -> str:
    if override is not None:
        return override
    extensions = (card or {}).get("extensions")
    orb = extensions.get("orb") if isinstance(extensions, dict) else None
    profile = orb.get("public_profile") if isinstance(orb, dict) else None
    if not isinstance(profile, dict):
        return ""
    lines = []
    for label, key in (("Appearance", "appearance"), ("Role", "role")):
        value = profile.get(key)
        if isinstance(value, str) and value.strip():
            lines.append(f"{label}: {value.strip()}")
    return "\n".join(lines)


def _private_sheet(card: Mapping | None) -> str:
    if not card:
        return ""
    parts = []
    if str(card.get("description") or "").strip():
        parts.append(str(card["description"]).strip())
    if str(card.get("personality") or "").strip():
        parts.append("Personality: " + str(card["personality"]).strip())
    return "\n\n".join(parts)


def _context_mode(conv: Mapping) -> GroupContextMode:
    """The conversation's character-context mode, defaulting on an unknown value.

    The column carries a CHECK constraint, so an out-of-domain value can only
    arrive from a hand-edited database; falling back to the behaviour-preserving
    default beats raising inside prompt assembly.
    """
    mode = str(conv.get("group_context_mode") or "private")
    return typed_cast(GroupContextMode, mode) if mode in ("private", "shared", "swap") else "private"


async def resolve_cast(conv: Mapping, *, speaker_member_id: str | None = None) -> TurnCast:
    """Resolve the active roster; synthesize the legacy member for solo chats."""
    if conv.get("kind", "solo") != "group":
        card_id = conv.get("character_card_id")
        card = await db.get_character_card(card_id) if card_id else None
        name = str((card or {}).get("name") or conv.get("character_name") or "Character")
        member = CastMember(
            member_id="",
            speaker_key="",
            card_id=card_id,
            name=name,
            kind="character",
            public_profile="",
            private_sheet=_private_sheet(card),
            mes_example=str((card or {}).get("mes_example") or ""),
            post_history=str((card or {}).get("post_history_instructions") or conv.get("post_history_instructions") or ""),
        )
        return TurnCast(False, (member,), member)

    members = await db.get_group_members(str(conv["id"]))
    resolved: list[CastMember] = []
    for member in members:
        card_id = member.get("character_card_id")
        card = await db.get_character_card(card_id) if card_id else None
        resolved.append(
            CastMember(
                member_id=member["id"],
                speaker_key=member["speaker_key"],
                card_id=card_id,
                name=member["display_name"],
                kind=member["member_kind"],
                public_profile=_public_profile(card, member.get("public_profile_override")),
                private_sheet=_private_sheet(card),
                mes_example=str((card or {}).get("mes_example") or ""),
                post_history=str((card or {}).get("post_history_instructions") or ""),
                scene_profile=str(member.get("public_profile_override") or ""),
                muted=bool(member.get("muted")),
            )
        )
    speaker = next((m for m in resolved if m.member_id == speaker_member_id), None)
    return TurnCast(True, tuple(resolved), speaker, _context_mode(conv))


async def historical_speaker_names(conversation_id: str, messages: Sequence[Mapping]) -> dict[str, str]:
    ids = {str(m["speaker_member_id"]) for m in messages if m.get("speaker_member_id")}
    if not ids:
        return {}
    members = await db.get_group_members(conversation_id, include_inactive=True)
    return {m["id"]: m["display_name"] for m in members if m["id"] in ids}


def parse_speaking_plan(raw: object, members: Sequence[Mapping], cap: int) -> list[tuple[Mapping, str]] | None:
    """Validate a Director plan. None means malformed/missing; [] is intentional rest."""
    if raw is None or not isinstance(raw, list):
        return None
    by_key = {str(m["speaker_key"]).casefold(): m for m in members if m.get("active") and not m.get("muted")}
    by_name = {str(m["display_name"]).casefold(): m for m in members if m.get("active") and not m.get("muted")}
    out: list[tuple[Mapping, str]] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        import re

        bits = re.split(r"\s*(?:—|:|-)\s*", item, maxsplit=1)
        who = bits[0].strip().casefold()
        beat = bits[1].strip() if len(bits) > 1 else ""
        member = by_key.get(who) or by_name.get(who)
        if member and (not out or out[-1][0]["id"] != member["id"]):
            out.append((member, beat))
        if len(out) >= cap:
            break
    return out if out or not raw else None


def round_robin_member(members: Sequence[Mapping], messages: Sequence[Mapping]) -> Mapping | None:
    eligible = [m for m in members if m.get("active") and not m.get("muted")]
    if not eligible:
        return None
    last_id = next((m.get("speaker_member_id") for m in reversed(messages) if m.get("speaker_member_id")), None)
    for index, member in enumerate(eligible):
        if member["id"] == last_id:
            return eligible[(index + 1) % len(eligible)]
    return eligible[0]
