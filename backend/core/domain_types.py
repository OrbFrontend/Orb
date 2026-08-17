"""Dependency-free aliases for closed string domains shared across layers."""

from __future__ import annotations

from typing import Literal, NamedTuple, TypeAlias

AgentLane: TypeAlias = Literal["writer", "agent"]
CompletionMode: TypeAlias = Literal["chat", "text"]
MessageRole: TypeAlias = Literal["user", "assistant"]


class CastMember(NamedTuple):
    member_id: str
    speaker_key: str
    card_id: str | None
    name: str
    kind: str
    public_profile: str
    private_sheet: str
    mes_example: str
    post_history: str


class TurnCast(NamedTuple):
    grouped: bool
    members: tuple[CastMember, ...]
    speaker: CastMember | None = None


__all__ = ["AgentLane", "CastMember", "CompletionMode", "MessageRole", "TurnCast"]
