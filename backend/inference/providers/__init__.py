"""Per-provider dialects of OpenAI's chat protocol, resolved from the route being called.

Whole protocols (Anthropic Messages, the Claude Code CLI) are separate transports and live beside this package.
"""

from __future__ import annotations

from collections.abc import Sequence

from .base import DeltaReader, Dialect, ModelProfile, is_forced_tool_choice
from .deepseek import DeepSeek
from .google import Google
from .nanogpt import NanoGPT
from .openrouter import OpenRouter

# Checked in order; the first match wins, and an unmatched route speaks plain OpenAI.
DIALECTS: tuple[Dialect, ...] = (Google(), DeepSeek(), NanoGPT(), OpenRouter())
_OPENAI = Dialect()


def resolve(route_url: str) -> Dialect:
    """The dialect of the route being called."""
    return next((dialect for dialect in DIALECTS if dialect.matches(route_url)), _OPENAI)


def openai_base_dialect(url: str) -> Dialect | None:
    """The dialect whose own OpenAI base path *url* carries, if any."""
    return next((dialect for dialect in DIALECTS if dialect.serves_openai_base(url)), None)


def decisions_path(netloc: str, segments: Sequence[str]) -> list[str] | None:
    """A provider's own decisions route for a chat-style configured URL, or ``None`` for the generic derivation."""
    return next((path for dialect in DIALECTS if (path := dialect.decisions_path(netloc, segments)) is not None), None)


__all__ = [
    "DIALECTS",
    "DeltaReader",
    "Dialect",
    "ModelProfile",
    "decisions_path",
    "is_forced_tool_choice",
    "openai_base_dialect",
    "resolve",
]
