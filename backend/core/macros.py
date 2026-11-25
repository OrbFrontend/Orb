"""Resolve prompt, message, and inline macros."""

from __future__ import annotations

import random
import re
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from datetime import datetime
from typing import Any, NamedTuple

from .settings import Settings

# Internal helpers


_LITERAL_RE = re.compile(r"`[^`\n]*`")


def _outside_literals(text: str, fn) -> str:
    """Apply *fn* to *text* with single-backtick spans masked out (kept literal).

    Spans are swapped for control-char placeholders, *fn* runs on the rest as one string (so occurrence counting spans the whole
    text), then the spans are restored verbatim -- backticks included, which is what makes literal macros survive repeated
    resolution passes.
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

    # Callable replacements: a name is inserted literally, never read as
    # ``re.sub`` template syntax (a persona called ``¯\_(ツ)_/¯`` would raise).
    def _fire(t: str) -> str:
        if user_name:
            t = re.sub(r"\{\{user\}\}", lambda _m: user_name, t, flags=re.IGNORECASE)
        if char_name:
            t = re.sub(r"\{\{char\}\}", lambda _m: char_name, t, flags=re.IGNORECASE)
        return t

    return _outside_literals(text, _fire)


_ORIGINAL_RE = re.compile(r"\{\{original\}\}", re.IGNORECASE)


def fill_original(text: str, original: str) -> str:
    """Substitute a card override's ``{{original}}`` with the text the override replaces (Character Card V2 spec).

    A card's ``system_prompt`` passes the Main prompt; its post-history instructions pass ``""``, Orb having no global
    instructions they could extend. Backticked spans stay literal; a filled text is stripped so an empty original leaves no
    stray blank lines (and an override of nothing but the macro renders nothing).
    """
    if not text or not _ORIGINAL_RE.search(text):
        return text or ""
    return _outside_literals(text, lambda t: _ORIGINAL_RE.sub(lambda _m: original, t)).strip()


def _sub_cast(text: str, cast_names: str) -> str:
    if not text or not cast_names:
        return text or ""
    return _outside_literals(text, lambda value: re.sub(r"\{\{cast\}\}", lambda _m: cast_names, value, flags=re.IGNORECASE))


_CARD_FIELD_RE = re.compile(r"\{\{(description|personality)\}\}", re.IGNORECASE)


def _sub_card_fields(text: str, description: str, personality: str) -> str:
    """Substitute {{description}} and {{personality}} in one pass; an empty field resolves to nothing.

    Each value drops its own nested macro and fills the other field's one level deep, with that field's card-field macros
    dropped, which cuts cycles. Backticked spans in a value stay literal. The result is complete: no later pass changes it.
    """
    if not text or not isinstance(text, str):
        return text or ""
    own = {
        name: re.sub(rf"\{{\{{{name}\}}\}}", "", value, flags=re.IGNORECASE)
        for name, value in (("description", description), ("personality", personality))
    }
    bare = {name: _outside_literals(value, lambda t: _CARD_FIELD_RE.sub("", t)) for name, value in own.items()}
    values = {
        name: _outside_literals(value, lambda t: _CARD_FIELD_RE.sub(lambda m: bare[m.group(1).lower()], t))
        for name, value in own.items()
    }

    def one(match: re.Match) -> str:
        return values[match.group(1).lower()]

    return _outside_literals(text, lambda t: _CARD_FIELD_RE.sub(one, t))


def card_description(card: Mapping[str, Any] | None) -> str:
    """The ``{{description}}`` value for a solo conversation: the card's ``description`` field alone."""
    return str((card or {}).get("description") or "").strip()


def card_personality(card: Mapping[str, Any] | None) -> str:
    """The ``{{personality}}`` value for a solo conversation: the card's ``personality`` field alone."""
    return str((card or {}).get("personality") or "").strip()


_COMMENT_BODY = r"\{\{//(?:\{\{[^{}]*\}\}|(?!\}\})[\s\S])*\}\}"
_COMMENT_RE = re.compile(rf"^[ \t]*(?:{_COMMENT_BODY}[ \t]*)+\r?\n|{_COMMENT_BODY}", re.MULTILINE)
_ROLL_RE = re.compile(r"\{\{roll::(\d+)d(\d+)\}\}", re.IGNORECASE)
_RANDOM_RE = re.compile(r"\{\{(?:random|pick)::(.*?)\}\}", re.IGNORECASE | re.DOTALL)
_TIME_RE = re.compile(r"\{\{time\}\}", re.IGNORECASE)
_DATE_RE = re.compile(r"\{\{date\}\}", re.IGNORECASE)
# Eats the newlines on both sides, joining what they separated. [\r\n] rather than \n because card fields commonly arrive CRLF.
_TRIM_RE = re.compile(r"[\r\n]*\{\{trim\}\}[\r\n]*", re.IGNORECASE)


def _comment(m: re.Match, rng: Any) -> str:
    return ""


# Rolling is a Python loop on the event loop, and card text can arrive from anywhere.
MAX_DICE = 1000


def _roll(m: re.Match, rng: Any) -> str:
    count, sides = int(m.group(1)), int(m.group(2))
    if sides < 1 or count > MAX_DICE:
        return m.group(0)  # no die to roll, or too many: left raw like any unresolvable macro
    return str(sum(rng.randint(1, sides) for _ in range(count)))


def _rand(m: re.Match, rng: Any) -> str:
    return rng.choice(m.group(1).split("::"))


def _time(m: re.Match, rng: Any) -> str:
    return datetime.now().strftime("%H:%M")


def _date(m: re.Match, rng: Any) -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _trim(m: re.Match, rng: Any) -> str:
    return ""


# The inline-macro grammar. Adding a macro = one regex + one handler + one row here; _resolve_inline and has_inline_macros
# iterate this table. Comments come first so a macro written inside one is deleted rather than resolved, and {{trim}} last so it
# eats the newlines the rows above leave behind.
_INLINE_MACROS: list[tuple[re.Pattern, Callable[[re.Match, Any], str]]] = [
    (_COMMENT_RE, _comment),
    (_ROLL_RE, _roll),
    (_RANDOM_RE, _rand),
    (_TIME_RE, _time),
    (_DATE_RE, _date),
    (_TRIM_RE, _trim),
]


_PICK_RE = re.compile(r"\{\{pick::", re.IGNORECASE)


def _resolve_inline(text: str, seed: str = "", pick_seed: str | None = None) -> str:
    """Resolve inline comment, roll, random/pick, time, date and trim macros.

    With a seed, random results depend on (seed, macro text, occurrence), keeping
    rebuilt prompt fields byte-stable. Empty seed rolls fresh; time always uses now.
    ``{{pick}}`` rolls with *pick_seed* (*seed* when None), so a pick can stay fixed where random and roll are fresh.
    """
    if not text or not isinstance(text, str):
        return text or ""
    if pick_seed is None:
        pick_seed = seed

    # Ordinal counts prior occurrences of the *same* macro text, so a seeded pick survives unrelated edits around it and repeats
    # of the same macro still roll independently.
    seen: dict[str, int] = {}

    def _rng(m: re.Match) -> Any:
        key = pick_seed if _PICK_RE.match(m.group(0)) else seed
        if not key:
            return random
        ordinal = seen.get(m.group(0), 0)
        seen[m.group(0)] = ordinal + 1
        return random.Random(f"{key}|{m.group(0)}|{ordinal}")

    def _fire(t: str) -> str:
        for pattern, handler in _INLINE_MACROS:
            t = pattern.sub(lambda m, h=handler: h(m, _rng(m)), t)
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


def outside_literals(text: str, fn: Callable[[str], str]) -> str:
    """Apply *fn* outside single-backtick literals, shared by all macro resolvers."""
    return _outside_literals(text, fn)


def resolve_message(text: str, user_name: str, char_name: str, seed: str = "") -> str:
    """Resolve all macros: {{user}}, {{char}}, and inline macros like {{roll}}.

    Use this for the latest user message, persona text, scenario, and other turn-specific content where all macros should be
    resolved. *seed* makes {{random}} and {{roll}} deterministic (see :func:`_resolve_inline`).
    """
    return _resolve_inline(_sub(text, user_name, char_name), seed=seed)


def resolve_inline(text: str, seed: str = "", *, pick_seed: str | None = None) -> str:
    """Resolve inline macros once at persistence; leave {{user}}/{{char}} for reads.

    Rolls are fresh unless *seed* is provided; ``{{pick}}`` rolls with *pick_seed* when given.
    """
    return _resolve_inline(text, seed=seed, pick_seed=pick_seed)


def has_inline_macros(text: str) -> bool:
    """True when *text* contains an inline macro, backticked literals excluded."""
    if not text or not isinstance(text, str):
        return False
    text = _LITERAL_RE.sub("", text)
    return any(pattern.search(text) for pattern, _ in _INLINE_MACROS)


def named_placeholders(text: str, names: Iterable[str]) -> frozenset[str]:
    """Which of *names* *text* names as ``{{name}}`` (case-insensitive, reported lowercase), outside backticked literals and
    ``{{// ... }}`` notes: literals are masked first, then notes removed, as inline resolution treats them."""
    if not text or not isinstance(text, str):
        return frozenset()
    pattern = re.compile(r"\{\{(" + "|".join(re.escape(name) for name in names) + r")\}\}", re.IGNORECASE)
    scanned = _COMMENT_RE.sub("", _LITERAL_RE.sub("\x00", text))
    return frozenset(match.group(1).lower() for match in pattern.finditer(scanned))


def has_clock_macros(text: str) -> bool:
    """True when *text* names ``{{time}}`` or ``{{date}}``, which read the clock on every resolve, backticked literals
    excluded."""
    text = _LITERAL_RE.sub("", text or "")
    return bool(_TIME_RE.search(text) or _DATE_RE.search(text))


def resolve_stored_random(texts: Sequence[str], choices: MutableMapping[str, str], key_prefix: str) -> list[str]:
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
    """Resolve only {{user}}/{{char}} placeholders -- no inline macros.

    Use this for historical messages and prompt context where inline macros (like {{roll}}) should NOT fire.
    """
    return _sub(text, user_name, char_name)


# Macros class


class Macros(NamedTuple):
    """Resolve description, personality, names, cast and inline macros for a turn.

    A value rolls once, where it is defined: the seed is the conversation's, narrowed to one place by :meth:`at`, so random,
    pick and roll results stay stable across prompt rebuilds and two places roll independently. ``fresh`` rolls random and
    roll anew on every resolve while a pick stays fixed for its place; an empty seed rolls everything fresh.
    """

    user: str
    char: str
    seed: str = ""
    cast: str = ""
    # {{description}} and {{personality}}: the card's own fields, from :func:`card_description` and :func:`card_personality` solo
    # and from the member in a group (``prompting.group_context.member_macros``). Unlike the three above they are bodies of
    # text, not names -- see :func:`_sub_card_fields`. :meth:`with_card_fields` resolves them once, so inserting one anywhere
    # reuses its rolls.
    description: str = ""
    personality: str = ""
    fresh: bool = False

    def at(self, place: str) -> Macros:
        """These macros seeded for one place: a field, a block, a lore entry. Places roll independently of each other."""
        return self._replace(seed=f"{self.seed}|{place}") if self.seed else self

    def with_card_fields(self, description: str, personality: str, place: str = "") -> Macros:
        """These macros carrying *description* and *personality* resolved once, each at its own place under *place*.

        Each field fills the other's macro with that field resolved alone (its card-field macros dropped, which cuts cycles)
        and drops its own. Resolving happens here, with this identity, and is seeded whatever ``fresh`` says: a card field is
        fixed for its place, and every ``{{description}}`` inserts these bytes.
        """
        bare = self._replace(description="", personality="", fresh=False)
        alone_description = bare.at(f"{place}description").resolve_message(description)
        alone_personality = bare.at(f"{place}personality").resolve_message(personality)
        return self._replace(
            description=bare._replace(personality=alone_personality).at(f"{place}description").resolve_message(description),
            personality=bare._replace(description=alone_description).at(f"{place}personality").resolve_message(personality),
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        char_name: str,
        active_persona: Mapping[str, Any] | None = None,
        seed: str = "",
        cast: str = "",
        description: str = "",
        personality: str = "",
    ) -> Macros:
        user = active_persona.get("name", "User") if active_persona else settings.get("user_name", "User")
        return cls(user=user, char=char_name, seed=seed, cast=cast, description=description, personality=personality)

    def resolve_message(self, text: str) -> str:
        """Full macro resolution ({{description}}, {{personality}}, {{user}}/{{char}}, {{cast}} + inline).

        Names go in before inline macros, so any of them can be a {{random}} option.
        """
        named = _sub_cast(_sub(_sub_card_fields(text, self.description, self.personality), self.user, self.char), self.cast)
        return _resolve_inline(named, seed="" if self.fresh else self.seed, pick_seed=self.seed)

    def resolve_prompt(self, text: str) -> str:
        """Only substitution -- {{description}}, {{personality}}, {{user}}/{{char}}, {{cast}} (no inline macros)."""
        fields = _sub_card_fields(text, self.description, self.personality)
        return _sub_cast(resolve_prompt(fields, self.user, self.char), self.cast)

    def _resolve_prompt_on_message(self, msg: Mapping[str, Any]) -> dict:
        """Apply prompt-level resolution (substitution only) to a single message dict."""
        return {**msg, "content": _apply_content(msg.get("content"), lambda t: self.resolve_prompt(t))}

    def resolve_prompt_messages(self, messages: Sequence[Mapping[str, Any]]) -> list[dict]:
        """Resolve prompt macros in a message sequence."""
        return [self._resolve_prompt_on_message(m) for m in messages]
