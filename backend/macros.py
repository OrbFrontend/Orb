"""
macros.py — Macro resolution for prompts and messages.

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
    Macros.wrap_client(client)        — wraps LLMClient for prompt-level resolution
    Macros.from_settings(...)         — factory from app settings
"""

from __future__ import annotations

import random
import re
from typing import Any, Mapping, NamedTuple, Sequence

from .llm_client import LLMClient


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

    def _resolve_prompt_on_message(self, msg: dict) -> dict:
        """Apply prompt-level resolution (substitution only) to a single message dict."""
        return {
            **msg,
            "content": _apply_content(msg.get("content"), lambda t: self.resolve_prompt(t)),
        }

    def resolve_prompt_messages(self, messages: list[dict]) -> list[dict]:
        """Apply prompt-level resolution to a list of message dicts."""
        return [self._resolve_prompt_on_message(m) for m in messages]

    def wrap_client(self, client: LLMClient) -> "_PlaceholderClient":
        return _PlaceholderClient(client, self.user, self.char)


class _PlaceholderClient(LLMClient):
    """Wraps LLMClient to resolve {{user}}/{{char}} on all messages before completion.

    Only applies prompt-level resolution (no inline macros) — inline macros
    must be resolved on the latest user message before it reaches this client.
    """

    def __init__(self, inner: LLMClient, user_name: str, char_name: str) -> None:
        self._inner = inner
        self._user_name = user_name
        self._char_name = char_name

    def abort(self) -> None:
        self._inner.abort()

    @property
    def is_aborted(self) -> bool:
        return self._inner.is_aborted

    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        model: str,
        tools: list[dict] | None = None,
        tool_choice: dict | str | None = None,
        **params,
    ):
        msgs = [
            {
                **msg,
                "content": _apply_content(
                    msg.get("content"),
                    lambda t: resolve_prompt(t, self._user_name, self._char_name),
                ),
            }
            for msg in messages
        ]
        async for item in self._inner.complete(msgs, model, tools=tools, tool_choice=tool_choice, **params):
            yield item
