"""Dependency-free aliases for closed string domains shared across layers."""

from __future__ import annotations

from typing import Literal, NamedTuple, TypeAlias

AgentLane: TypeAlias = Literal["writer", "agent"]
CompletionMode: TypeAlias = Literal["chat", "text"]
MessageRole: TypeAlias = Literal["user", "assistant"]

# Which character information every group generation carries. Stored on
# ``conversations.group_context_mode``; the projection each value selects lives
# in ``inference/group_context.py`` and nowhere else.
GroupContextMode: TypeAlias = Literal["private", "shared", "swap"]


class CastMember(NamedTuple):
    member_id: str
    speaker_key: str
    card_id: str | None
    name: str
    kind: str
    # The effective public projection: the scene override when one is set,
    # otherwise the card's confirmed ``extensions.orb.public_profile``.
    public_profile: str
    private_sheet: str
    mes_example: str
    post_history: str
    # The user-authored ``group_members.public_profile_override`` alone. Kept
    # apart from ``public_profile`` (which already resolves to it when set) so
    # a consumer can label the projection by provenance: a scene override is
    # local to this conversation, a card profile travels with the card.
    scene_profile: str = ""
    # In scene but never scheduled to speak. Muted members still contribute
    # their identity to the shared body — the cast has to know they are there —
    # but they can never be the *active* speaker, so they are excluded from
    # per-speaker maxima.
    muted: bool = False


class TurnCast(NamedTuple):
    grouped: bool
    members: tuple[CastMember, ...]
    speaker: CastMember | None = None
    # Ignored unless ``grouped``; solo turns always project as ``private``.
    context_mode: GroupContextMode = "private"


__all__ = ["AgentLane", "CastMember", "CompletionMode", "GroupContextMode", "MessageRole", "TurnCast"]
