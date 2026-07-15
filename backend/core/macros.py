"""
macros.py — Macro resolution for prompts and messages.

A dependency-free leaf: it turns ``{{user}}``/``{{char}}`` and inline macros
like ``{{roll}}`` into literal text and imports nothing else in the codebase.
It knows about *strings and message dicts*, not about the LLM client — the
pipeline applies :meth:`Macros.resolve_prompt_messages` at the transport
boundary (the cached-base ``resolve`` hook in ``cached_call.py``) rather than
this module reaching up into the client layer.

Public API:
    resolve_message(text, user_name, char_name, seed="") — Full resolution
        ({{user}}/{{char}} + inline macros like {{roll}} and {{random}}).
        Use for: the latest user message, persona, scenario, and other
        prompt text that should have all macros resolved. A non-empty
        *seed* makes {{random}} deterministic (see below); {{roll}} always
        rolls fresh.

    resolve_prompt(text, user_name, char_name) — Substitution only
        ({{user}}/{{char}}, no inline macros).
        Use for: historical messages and prompt context where inline
        macros should NOT fire.

    resolve_inline(text) — Inline macros only ({{roll}}/{{random}}, fresh
        rolls; no {{user}}/{{char}}). The persist-boundary entry: message
        content is resolved once with this right before it is written to
        the DB, so stored history never re-rolls.

    has_inline_macros(text) — True when *text* contains an inline macro.

    resolve_stored_random(texts, choices, key_prefix) — {{random}} with a
        per-conversation choice map. Used for global rows (mood/interactive
        fragments) whose source text cannot be rewritten: the first
        resolution rolls and records into *choices*; later turns reuse the
        stored pick so the fragment stays fixed for the conversation.

Inline macros:
    {{roll::NdM}}                 — sum of N M-sided dice, fresh every call.
    {{random::opt1::opt2::...}}   — one option, ``::``-separated. Options
        cannot contain ``::`` or ``}}`` (grammar, not enforced). With a
        seed (or a stored choice) the pick is stable; unseeded it re-rolls.

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
from typing import Any, Mapping, MutableMapping, NamedTuple, Sequence

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
_RANDOM_RE = re.compile(r"\{\{random::(.*?)\}\}", re.IGNORECASE | re.DOTALL)


def _resolve_inline(text: str, seed: str = "") -> str:
    """Resolve inline macros: {{roll::2d6}} and {{random::a::b}}.

    {{roll}} always rolls fresh. {{random}} rolls fresh when *seed* is empty;
    with a seed the pick is a pure function of (seed, macro text, occurrence),
    so identical text resolves identically — used to keep {{random}} in
    per-turn-rebuilt prompt fields (persona, scenario) byte-stable per
    conversation instead of re-rolling and busting the shared KV prefix.
    """
    if not text or not isinstance(text, str):
        return text or ""

    def _roll(m: re.Match) -> str:
        count, sides = int(m.group(1)), int(m.group(2))
        return str(sum(random.randint(1, sides) for _ in range(count)))

    text = _ROLL_RE.sub(_roll, text)

    # Ordinal counts prior occurrences of the *same* macro text, so a seeded
    # pick survives unrelated edits around it and repeats of the same macro
    # still roll independently.
    seen: dict[str, int] = {}

    def _rand(m: re.Match) -> str:
        options = m.group(1).split("::")
        if seed:
            ordinal = seen.get(m.group(0), 0)
            seen[m.group(0)] = ordinal + 1
            return random.Random(f"{seed}|{m.group(0)}|{ordinal}").choice(options)
        return random.choice(options)

    return _RANDOM_RE.sub(_rand, text)


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


def resolve_message(text: str, user_name: str, char_name: str, seed: str = "") -> str:
    """Resolve all macros: {{user}}, {{char}}, and inline macros like {{roll}}.

    Use this for the latest user message, persona text, scenario, and other
    turn-specific content where all macros should be resolved. *seed* makes
    {{random}} deterministic (see :func:`_resolve_inline`).
    """
    return _resolve_inline(_sub(text, user_name, char_name), seed=seed)


def resolve_inline(text: str) -> str:
    """Fire inline macros ({{roll}}, {{random}}) with fresh rolls; no {{user}}/{{char}}.

    The persist-boundary entry: user/assistant message content and greetings
    are resolved once with this right before the DB write, so stored history
    holds the final text and never re-rolls. {{user}}/{{char}} stay raw in
    storage — the display and prompt paths substitute them on read.
    """
    return _resolve_inline(text)


def has_inline_macros(text: str) -> bool:
    """True when *text* contains an inline macro ({{roll}} or {{random}})."""
    if not text or not isinstance(text, str):
        return False
    return bool(_ROLL_RE.search(text) or _RANDOM_RE.search(text))


def resolve_stored_random(
    texts: Sequence[str],
    choices: MutableMapping[str, str],
    key_prefix: str,
) -> list[str]:
    """Resolve {{random}} in *texts* against a per-conversation choice map.

    For global rows (mood/interactive fragments) whose source text cannot be
    rewritten: each {{random}} occurrence gets the key ``f"{key_prefix}:{n}"``
    where *n* is one shared counter across all *texts* (so a fragment's fields
    keep stable keys in a fixed order). A stored choice is reused only while it
    is still one of the macro's current options; otherwise a fresh pick is
    rolled and recorded into *choices* (mutated in place). {{roll}} and
    {{user}}/{{char}} are left untouched.
    """
    counter = 0

    def _pick(m: re.Match) -> str:
        nonlocal counter
        key = f"{key_prefix}:{counter}"
        counter += 1
        options = m.group(1).split("::")
        stored = choices.get(key)
        if stored is not None and stored in options:
            return stored
        choice = random.choice(options)
        choices[key] = choice
        return choice

    resolved = []
    for text in texts:
        if not text or not isinstance(text, str):
            resolved.append(text or "")
        else:
            resolved.append(_RANDOM_RE.sub(_pick, text))
    return resolved


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
    """Resolve {{user}}/{{char}} and inline macros for a conversation turn.

    *seed* (normally the conversation id) makes {{random}} deterministic in
    :meth:`resolve_message`, so per-turn-rebuilt prompt fields (persona,
    scenario) resolve to the same bytes every turn of a conversation instead
    of re-rolling and busting the shared KV prefix. Empty seed = fresh rolls.
    """

    user: str
    char: str
    seed: str = ""

    @classmethod
    def from_settings(
        cls,
        settings: Mapping[str, Any],
        char_name: str,
        active_persona: Mapping[str, Any] | None = None,
        seed: str = "",
    ) -> "Macros":
        user = active_persona.get("name", "User") if active_persona else settings.get("user_name", "User")
        return cls(user=user, char=char_name, seed=seed)

    def resolve_message(self, text: str) -> str:
        """Full macro resolution ({{user}}/{{char}} + inline) for a text string."""
        return resolve_message(text, self.user, self.char, seed=self.seed)

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
