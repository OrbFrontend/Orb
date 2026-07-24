"""Dependency-free aliases for closed string domains shared across layers."""

from __future__ import annotations

from typing import Literal, TypeAlias

AgentLane: TypeAlias = Literal["writer", "agent"]
CompletionMode: TypeAlias = Literal["chat", "text"]
MessageRole: TypeAlias = Literal["user", "assistant"]

__all__ = ["AgentLane", "CompletionMode", "MessageRole"]
