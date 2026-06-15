"""
macros.py — Macro resolution for prompts and messages.

A dependency-free leaf: it turns ``{{user}}``/``{{char}}`` and inline macros
like ``{{roll}}`` into literal text and imports nothing else in the codebase.
It knows about *strings and message dicts*, not about the LLM client — the
pipeline applies :meth:`Macros.resolve_prompt_messages` at the transport
boundary (the cached-base ``resolve`` hook in ``cached_call.py``) rather than
this module reaching up into the client layer.

Public API:
    resolve_message(text, user_name, char_name) — Full resolution
        ({{user}}/{{char}} + inline macros like {{roll}}).
        Use for: the latest user message, persona, scenario, and other
        prompt text that should have all macros resolved.

    resolve_prompt(text, user_name, char_name) — Substitution only
        ({{user}}/{{char}}, no inline macros).
        Use for: historical messages and prompt context where inline
        macros should NOT fire.

    Macros.resolve_message(text)      — instance method, full resolution
    Macros.resolve_prompt(text)       — instance method, substitution only
    Macros.resolve_prompt_messages(msgs) — batch prompt-level res on message list
        (the transport-boundary catch-all that guarantees no placeholder
        reaches the model, whatever a pass assembled)
    Macros.from_settings(...)         — factory from app settings
"""

from __future__ import annotations

import random
import re
from typing import Any, Mapping, NamedTuple, Sequence

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _sub(text: str, user_name: str, char_name: str) -> str:
    """Replace {{user}} and {{char}} placeholders (case-insensitive)."""
    if not text or not isinstance(text, str):
        return text or ""
    if user_name:
        text = re.sub(r"\{\{user\}\}", user_name, text, flags=re.IGNORECASE)
    if char_name:
        text = re.sub(r"\{\{char\}\}", char_name, text, flags=re.IGNORECASE)
    return text


_ROLL_RE = re.compile(r"\{\{roll::(\d+)d(\d+)\}\}", re.IGNORECASE)


def _resolve_inline(text: str) -> str:
    """Resolve inline macros such as {{roll::2d6}}."""
    if not text or not isinstance(text, str):
        return text or ""

    def _roll(m: re.Match) -> str:
        count, sides = int(m.group(1)), int(m.group(2))
        return str(sum(random.randint(1, sides) for _ in range(count)))

    return _ROLL_RE.sub(_roll, text)


def _apply_content(content: str | list | None, fn) -> str | list | None:
    """Apply a text transform to a message content field."""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        return [{**part, "text": fn(part["text"])} if part.get("type") == "text" else part for part in content]
    return content


# ---------------------------------------------------------------------------
# Module-level functions
# ---------------------------------------------------------------------------


def resolve_message(text: str, user_name: str, char_name: str) -> str:
    """Resolve all macros: {{user}}, {{char}}, and inline macros like {{roll}}.

    Use this for the latest user message, persona text, scenario, and other
    turn-specific content where all macros should be resolved.
    """
    return _resolve_inline(_sub(text, user_name, char_name))


def resolve_prompt(text: str, user_name: str, char_name: str) -> str:
    """Resolve only {{user}}/{{char}} placeholders — no inline macros.

    Use this for historical messages and prompt context where inline macros
    (like {{roll}}) should NOT fire.
    """
    return _sub(text, user_name, char_name)


# ---------------------------------------------------------------------------
# Macros class
# ---------------------------------------------------------------------------


class Macros(NamedTuple):
    """Resolve {{user}}/{{char}} and inline macros for a conversation turn."""

    user: str
    char: str

    @classmethod
    def from_settings(
        cls,
        settings: Mapping[str, Any],
        char_name: str,
        active_persona: Mapping[str, Any] | None = None,
    ) -> "Macros":
        user = active_persona.get("name", "User") if active_persona else settings.get("user_name", "User")
        return cls(user=user, char=char_name)

    def resolve_message(self, text: str) -> str:
        """Full macro resolution ({{user}}/{{char}} + inline) for a text string."""
        return resolve_message(text, self.user, self.char)

    def resolve_prompt(self, text: str) -> str:
        """Only {{user}}/{{char}} substitution (no inline macros)."""
        return resolve_prompt(text, self.user, self.char)

    def _resolve_prompt_on_message(self, msg: Mapping[str, Any]) -> dict:
        """Apply prompt-level resolution (substitution only) to a single message dict."""
        return {
            **msg,
            "content": _apply_content(msg.get("content"), lambda t: self.resolve_prompt(t)),
        }

    def resolve_prompt_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict]:
        """Apply prompt-level resolution to every message in a list.

        This is the transport-boundary catch-all: passed to a cached base's
        ``resolve`` hook so the fully-assembled wire messages are scrubbed of
        ``{{user}}``/``{{char}}`` just before they are sent, no matter which
        pass built them (e.g. the director's tool prompt embeds user-authored
        fragment text that can carry ``{{char}}``). Inline macros like
        ``{{roll}}`` are intentionally *not* fired here — those are resolved on
        the latest user message and prefix content when it is built.
        """
        return [self._resolve_prompt_on_message(m) for m in messages]
