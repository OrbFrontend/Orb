"""Resolve prompt, message, and inline macros."""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from datetime import datetime
from typing import Any, NamedTuple

# Internal helpers


_LITERAL_RE = re.compile(r"`[^`\n]*`")


def _outside_literals(text: str, fn) -> str:
    """Apply *fn* to *text* with single-backtick spans masked out (kept literal).

    Spans are swapped for control-char placeholders, *fn* runs on the rest as
    one string (so occurrence counting spans the whole text), then the spans
    are restored verbatim — backticks included, which is what makes literal
    macros survive repeated resolution passes.
    """
    if "`" not in text:
        return fn(text)
    spans: list[str] = []

    def _stash(m: re.Match) -> str:
        spans.append(m.group(0))
        return f"\x00{len(spans) - 1}\x01"

    masked = _LITERAL_RE.sub(_stash, text)
    return re.sub(r"\x00(\d+)\x01", lambda m: spans[int(m.group(1))], fn(masked))


def _sub(text: str, user_name: str, char_name: str) -> str:
    """Replace {{user}} and {{char}} placeholders (case-insensitive); backticked spans stay literal."""
    if not text or not isinstance(text, str):
        return text or ""

    def _fire(t: str) -> str:
        if user_name:
            t = re.sub(r"\{\{user\}\}", user_name, t, flags=re.IGNORECASE)
        if char_name:
            t = re.sub(r"\{\{char\}\}", char_name, t, flags=re.IGNORECASE)
        return t

    return _outside_literals(text, _fire)


def _sub_cast(text: str, cast_names: str) -> str:
    if not text or not cast_names:
        return text or ""
    return _outside_literals(text, lambda value: re.sub(r"\{\{cast\}\}", cast_names, value, flags=re.IGNORECASE))


# Two branches: a comment that owns its line(s) takes the whole line with it (no
# blank line left behind); one sitting mid-line takes only itself, leaving the
# surrounding spaces. Non-greedy either way, so the body ends at the first `}}`.
_COMMENT_RE = re.compile(r"^[ \t]*\{\{//.*?\}\}[ \t]*\n|\{\{//.*?\}\}", re.DOTALL | re.MULTILINE)
_ROLL_RE = re.compile(r"\{\{roll::(\d+)d(\d+)\}\}", re.IGNORECASE)
_RANDOM_RE = re.compile(r"\{\{(?:random|pick)::(.*?)\}\}", re.IGNORECASE | re.DOTALL)
_TIME_RE = re.compile(r"\{\{time\}\}", re.IGNORECASE)
_DATE_RE = re.compile(r"\{\{date\}\}", re.IGNORECASE)


def _comment(m: re.Match, rng: Any) -> str:
    return ""


def _roll(m: re.Match, rng: Any) -> str:
    count, sides = int(m.group(1)), int(m.group(2))
    return str(sum(rng.randint(1, sides) for _ in range(count)))


def _rand(m: re.Match, rng: Any) -> str:
    return rng.choice(m.group(1).split("::"))


def _time(m: re.Match, rng: Any) -> str:
    return datetime.now().strftime("%H:%M")


def _date(m: re.Match, rng: Any) -> str:
    return datetime.now().strftime("%Y-%m-%d")


# The inline-macro grammar. Adding a macro = one regex + one handler + one row
# here; _resolve_inline and has_inline_macros iterate this table. Comments come
# first so a macro written inside one is deleted rather than resolved.
_INLINE_MACROS: list[tuple[re.Pattern, Callable[[re.Match, Any], str]]] = [
    (_COMMENT_RE, _comment),
    (_ROLL_RE, _roll),
    (_RANDOM_RE, _rand),
    (_TIME_RE, _time),
    (_DATE_RE, _date),
]


def _resolve_inline(text: str, seed: str = "") -> str:
    """Resolve inline macros ({{//}}, {{roll}}, {{random}}/{{pick}}, {{time}}).

    Randomized macros roll fresh when *seed* is empty; with a seed the result
    is a pure function of (seed, macro text, occurrence), so identical text
    resolves identically — used to keep inline macros in per-turn-rebuilt
    prompt fields (persona, scenario) byte-stable per conversation instead of
    re-rolling and busting the shared KV prefix. {{time}} takes no randomness
    and always resolves to the current time, seed or not.
    """
    if not text or not isinstance(text, str):
        return text or ""

    # Ordinal counts prior occurrences of the *same* macro text, so a seeded
    # pick survives unrelated edits around it and repeats of the same macro
    # still roll independently.
    seen: dict[str, int] = {}

    def _seeded_rng(m: re.Match) -> random.Random:
        ordinal = seen.get(m.group(0), 0)
        seen[m.group(0)] = ordinal + 1
        return random.Random(f"{seed}|{m.group(0)}|{ordinal}")

    def _fire(t: str) -> str:
        for pattern, handler in _INLINE_MACROS:
            t = pattern.sub(lambda m, h=handler: h(m, _seeded_rng(m) if seed else random), t)
        return t

    return _outside_literals(text, _fire)


def _apply_content(content: str | list | None, fn) -> str | list | None:
    """Apply a text transform to a message content field."""
    if isinstance(content, str):
        return fn(content)
    if isinstance(content, list):
        return [{**part, "text": fn(part["text"])} if part.get("type") == "text" else part for part in content]
    return content


# Module-level functions


def resolve_message(text: str, user_name: str, char_name: str, seed: str = "") -> str:
    """Resolve all macros: {{user}}, {{char}}, and inline macros like {{roll}}.

    Use this for the latest user message, persona text, scenario, and other
    turn-specific content where all macros should be resolved. *seed* makes
    {{random}} and {{roll}} deterministic (see :func:`_resolve_inline`).
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
    """True when *text* contains an inline macro, backticked literals excluded."""
    if not text or not isinstance(text, str):
        return False
    text = _LITERAL_RE.sub("", text)
    return any(pattern.search(text) for pattern, _ in _INLINE_MACROS)


def resolve_stored_random(
    texts: Sequence[str],
    choices: MutableMapping[str, str],
    key_prefix: str,
) -> list[str]:
    """Resolve random and pick macros against stored choices."""
    seen: dict[str, int] = {}

    def _pick(m: re.Match) -> str:
        ordinal = seen.get(m.group(0), 0)
        seen[m.group(0)] = ordinal + 1
        key = f"{key_prefix}:{m.group(0)}:{ordinal}"
        stored = choices.get(key)
        if stored is not None:
            return stored
        choice = random.choice(m.group(1).split("::"))
        choices[key] = choice
        return choice

    resolved = []
    for text in texts:
        if not text or not isinstance(text, str):
            resolved.append(text or "")
        else:
            resolved.append(_outside_literals(text, lambda t: _RANDOM_RE.sub(_pick, t)))
    return resolved


def resolve_prompt(text: str, user_name: str, char_name: str) -> str:
    """Resolve only {{user}}/{{char}} placeholders — no inline macros.

    Use this for historical messages and prompt context where inline macros
    (like {{roll}}) should NOT fire.
    """
    return _sub(text, user_name, char_name)


# Macros class


class Macros(NamedTuple):
    """Resolve {{user}}/{{char}} and inline macros for a conversation turn.

    *seed* (normally the conversation id) makes {{random}} and {{roll}}
    deterministic in :meth:`resolve_message`, so per-turn-rebuilt prompt
    fields (persona, scenario) resolve to the same bytes every turn of a
    conversation instead of re-rolling and busting the shared KV prefix.
    Empty seed = fresh rolls.
    """

    user: str
    char: str
    seed: str = ""
    cast: str = ""

    @classmethod
    def from_settings(
        cls,
        settings: Mapping[str, Any],
        char_name: str,
        active_persona: Mapping[str, Any] | None = None,
        seed: str = "",
        cast: str = "",
    ) -> Macros:
        user = active_persona.get("name", "User") if active_persona else settings.get("user_name", "User")
        return cls(user=user, char=char_name, seed=seed, cast=cast)

    def resolve_message(self, text: str) -> str:
        """Full macro resolution ({{user}}/{{char}} + inline) for a text string."""
        return _sub_cast(resolve_message(text, self.user, self.char, seed=self.seed), self.cast)

    def resolve_prompt(self, text: str) -> str:
        """Only {{user}}/{{char}} substitution (no inline macros)."""
        return _sub_cast(resolve_prompt(text, self.user, self.char), self.cast)

    def _resolve_prompt_on_message(self, msg: Mapping[str, Any]) -> dict:
        """Apply prompt-level resolution (substitution only) to a single message dict."""
        return {
            **msg,
            "content": _apply_content(msg.get("content"), lambda t: self.resolve_prompt(t)),
        }

    def resolve_prompt_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict]:
        """Resolve prompt macros in a message sequence."""
        return [self._resolve_prompt_on_message(m) for m in messages]
