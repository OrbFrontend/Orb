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
        *seed* makes {{random}} and {{roll}} deterministic (see below).

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

Inline macros (adding one = a regex + a handler + a row in _INLINE_MACROS):
    {{roll::NdM}}                 — sum of N M-sided dice.
    {{random::opt1::opt2::...}}   — one option, ``::``-separated. Options
        cannot contain ``::`` or ``}}`` (grammar, not enforced).
    {{pick::opt1::opt2::...}}     — alias of {{random}}, same grammar.
    {{time}}                      — current local time, HH:MM.
    {{date}}                      — current local date, YYYY-MM-DD.
    Time/date ignore the seed: they always resolve to *now*, so in
        per-turn-rebuilt prompt text they change bytes over time (KV-cache
        bust — prefer them in messages, where they freeze at the persist
        boundary).
    Roll/random/pick re-roll on every unseeded resolution; with a seed (or a
    stored choice) the result is stable — so a roll or pick in
    per-turn-rebuilt prompt text (persona, scenario) stays fixed for the
    conversation.

Literal escape: any macro inside a single-backtick span (`{{random::a::b}}`)
    is left untouched by every resolver, {{user}}/{{char}} included. The
    backticks stay in the text, so literalness survives repeated resolution
    passes — used to show macro syntax to a model (e.g. instructing the
    Director to emit a macro) without it firing en route.

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
from datetime import datetime
from typing import Any, Callable, Mapping, MutableMapping, NamedTuple, Sequence

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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


_ROLL_RE = re.compile(r"\{\{roll::(\d+)d(\d+)\}\}", re.IGNORECASE)
_RANDOM_RE = re.compile(r"\{\{(?:random|pick)::(.*?)\}\}", re.IGNORECASE | re.DOTALL)
_TIME_RE = re.compile(r"\{\{time\}\}", re.IGNORECASE)
_DATE_RE = re.compile(r"\{\{date\}\}", re.IGNORECASE)


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
# here; _resolve_inline and has_inline_macros iterate this table.
_INLINE_MACROS: list[tuple[re.Pattern, Callable[[re.Match, Any], str]]] = [
    (_ROLL_RE, _roll),
    (_RANDOM_RE, _rand),
    (_TIME_RE, _time),
    (_DATE_RE, _date),
]


def _resolve_inline(text: str, seed: str = "") -> str:
    """Resolve inline macros ({{roll}}, {{random}}/{{pick}}, {{time}}).

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


# ---------------------------------------------------------------------------
# Module-level functions
# ---------------------------------------------------------------------------


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
    """Resolve {{random}}/{{pick}} in *texts* against a per-conversation choice map.

    For global rows (mood/interactive fragments) whose source text cannot be
    rewritten: each {{random}} occurrence gets the key
    ``f"{key_prefix}:{macro_text}:{ordinal}"`` where *ordinal* counts prior
    occurrences of the same macro text across all *texts* — the same scheme as
    the seeded path in :func:`_resolve_inline`, so a pick survives edits around
    it and can never be claimed by a different macro. Because the key embeds
    the exact macro text, a stored pick is always one of the current options
    and is reused verbatim; editing a macro's options changes its key, so the
    edited macro re-rolls fresh (and the old key goes stale, harmlessly).
    Fresh picks are recorded into *choices* (mutated in place). {{roll}} and
    {{user}}/{{char}} are left untouched.
    """
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


# ---------------------------------------------------------------------------
# Macros class
# ---------------------------------------------------------------------------


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
