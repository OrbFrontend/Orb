from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ....core import AFTER_DIRECTOR, CastMember, DecisionDefinition, Macros, outside_literals, resolve_inline
from ....prompting import format_message_with_attachments, group_speaker_label, member_place

DECISION_RENDERER_VERSION = "1"
_RECENT_HISTORY_DEPTH = 6
# {{recent_history::N}} renders the last N messages instead of the default.
_MAX_RECENT_HISTORY_DEPTH = 50
STATE_MACROS = frozenset(
    {"last_message", "last_assistant_message", "recent_history", "state", "user", "char", "cast", "description", "personality"}
)
TEXT_MACROS = frozenset({"user", "char", "cast"})
# Names, resolved before inline macros as the main prompt path does, so they can
# sit inside a {{random}} option. Message bodies are inserted after, and never can.
_IDENTITY_MACROS = TEXT_MACROS
# What each macro produced by a later stage needs to have run first.
LATER_STAGE_MACROS: Mapping[str, str] = {"scene_guidance": "the Director", "draft": "the Writer"}
# The Situation macros of a decision placed after the Director: the Director's plan joins the conversation.
AFTER_DIRECTOR_MACROS = frozenset({"scene_guidance"})
# Inline macros resolve in every decision field; listed here as authors write them.
INLINE_MACROS = ("roll::1d20", "random::a::b", "pick::a::b", "time", "date", "trim", "// note")
# State macros that take a "::" argument, as authors write them.
STATE_MACRO_FORMS = ("recent_history::10",)
_MACRO_RE = re.compile(r"\{\{\s*([a-z_]+)\s*(?:::([^{}]*))?\}\}", re.IGNORECASE)
_NESTED_RE = re.compile(r"\{\{(?:random|pick|roll)::[^{}]*\{\{\s*([a-z_]+)\s*(?:::[^{}]*)?\}\}", re.IGNORECASE)
# Validation resolves inline macros only to remove them; any fixed seed will do.
_VALIDATION_SEED = "validate"


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    last_message: str = ""
    last_assistant_message: str = ""
    # Labelled completed messages, oldest first, up to _MAX_RECENT_HISTORY_DEPTH.
    history: tuple[str, ...] = ()
    user: str = ""
    char: str = ""
    cast: str = ""
    description: str | None = None
    personality: str | None = None
    # The Director's Scene Guidance body; None until the Director has run.
    scene_guidance: str | None = None
    # Every enabled state fragment's entries, without the Current State heading: the turn's starting state before the
    # Director, and with its before-Writer changes, as ``old -> new``, after it.
    state: str = ""
    scope: str = "solo"
    anchor_message_id: int | None = None
    seed: str = ""

    def recent_history(self, depth: int = _RECENT_HISTORY_DEPTH) -> str:
        return "\n\n".join(self.history[-depth:])

    def value(self, macro: str, depth: int = _RECENT_HISTORY_DEPTH) -> str | None:
        if macro == "recent_history":
            return self.recent_history(depth)
        return getattr(self, macro, None)

    def roll_seed(self, fragment_id: str | None, field: str) -> str:
        """Seed one field's inline macros: fixed within an exchange, fresh across exchanges.

        A regeneration renders the same bytes, so replay and the answer cache still match. ``None`` shares the seed across
        fragments -- the Situation's, so one exchange shows every question the same world and equal templates still batch. No
        seed means fresh rolls.
        """
        return f"{self.seed}|{self.anchor_message_id}|{fragment_id or ''}|{field}" if self.seed else ""

    def pick_seed(self, fragment_id: str | None, field: str) -> str:
        """Seed one field's ``{{pick}}``: fixed for the conversation, as a pick is everywhere, where random and roll follow
        :meth:`roll_seed`."""
        return f"{self.seed}|judge|{fragment_id or ''}|{field}" if self.seed else ""


def _card_fields(
    macros: Macros, description: str | None, personality: str | None, place: str = ""
) -> tuple[str | None, str | None]:
    """The card fields as the Writer's ``{{description}}`` and ``{{personality}}`` insert them, rolls included; an absent
    field stays as it is."""
    fields = macros.with_card_fields(description or "", personality or "", place)
    return (fields.description if description else description, fields.personality if personality else personality)


def _message_text(message: Mapping[str, Any], macros: Macros | None, scripts: Any = None) -> str:
    content = format_message_with_attachments(message, macros, scripts)["content"]
    return content if isinstance(content, str) else "\n\n".join(part["text"] for part in content if part["type"] == "text")


def _labelled(message: Mapping[str, Any], text: str, names: Mapping[str, str], grouped: bool) -> str:
    if message.get("role") != "assistant":
        return f"User: {text}"
    label = group_speaker_label(names, message.get("speaker_member_id")) if grouped else "Assistant"
    return f"{label}: {text}"


def build_snapshot(
    *,
    history: Sequence[Mapping[str, Any]],
    current_request: str,
    macros: Macros,
    scope: str,
    speaker_names: Mapping[str, str] | None = None,
    scripts: Any = None,
    speaker_scripts: Mapping[str, Any] | None = None,
    description: str | None = None,
    personality: str | None = None,
    anchor_message_id: int | None = None,
    state: str = "",
) -> DecisionSnapshot:
    grouped = scope == "group"
    names = speaker_names or {}
    scoped_scripts = speaker_scripts or {}

    def scripts_for(message: Mapping[str, Any]) -> Any:
        return scoped_scripts.get(str(message.get("speaker_member_id"))) if grouped else scripts

    completed = [message for message in history if message.get("role") in ("user", "assistant")]
    last_assistant = next((message for message in reversed(completed) if message.get("role") == "assistant"), None)
    resolved_description, resolved_personality = _card_fields(macros, description, personality)
    previous_reply = ""
    if last_assistant:
        body = _message_text(last_assistant, macros, scripts_for(last_assistant))
        previous_reply = f"{group_speaker_label(names, last_assistant.get('speaker_member_id'))}: {body}" if grouped else body
    return DecisionSnapshot(
        last_message=current_request,
        last_assistant_message=previous_reply,
        history=tuple(
            _labelled(message, _message_text(message, macros, scripts_for(message)), names, grouped)
            for message in completed[-_MAX_RECENT_HISTORY_DEPTH:]
        ),
        user=macros.user,
        char=macros.char,
        cast=macros.cast,
        description=resolved_description,
        personality=resolved_personality,
        scope=scope,
        anchor_message_id=anchor_message_id,
        seed=macros.seed,
        state=state,
    )


def card_snapshots(snapshot: DecisionSnapshot, members: Iterable[CastMember]) -> dict[str, DecisionSnapshot]:
    """Scope card-embedded decisions to each card's first cast member."""
    scoped: dict[str, DecisionSnapshot] = {}
    for member in members:
        if member.card_id and member.card_id not in scoped:
            # The member's places, so the Judge reads the rolls the cast section and the Writer show.
            member_macros = Macros(user=snapshot.user, char=member.name, seed=snapshot.seed, cast=snapshot.cast)
            description, personality = _card_fields(
                member_macros, member.description, member.personality, member_place(member, "")
            )
            scoped[member.card_id] = replace(snapshot, char=member.name, description=description, personality=personality)
    return scoped


def _depth(name: str, argument: str | None) -> int | None:
    """The message count *name* renders with *argument*, or None when that is no count.

    Only ``{{recent_history::N}}`` takes one; other macros render bare.
    """
    if argument is None:
        return _RECENT_HISTORY_DEPTH
    value = argument.strip()
    valid = name == "recent_history" and value.isdecimal() and 1 <= int(value) <= _MAX_RECENT_HISTORY_DEPTH
    return int(value) if valid else None


def _macro_uses(text: str) -> list[tuple[str, str | None]]:
    """Each macro in *text* with its "::" argument, in order, repeats included.

    An argument on a name outside STATE_MACROS is inline grammar left raw (an
    unrollable ``{{roll::0d6}}``), so only that name's bare form counts.
    """
    found: list[tuple[str, str | None]] = []

    def collect(body: str) -> str:
        for match in _MACRO_RE.finditer(body):
            name, argument = match.group(1).lower(), match.group(2)
            if argument is None or name in STATE_MACROS:
                found.append((name, argument))
        return body

    outside_literals(resolve_inline(text, seed=_VALIDATION_SEED), collect)
    return found


def macros_used(text: str) -> list[str]:
    return list(dict.fromkeys(name for name, _ in _macro_uses(text)))


def _nested_state_macros(text: str) -> list[str]:
    found: list[str] = []

    def collect(body: str) -> str:
        for match in _NESTED_RE.finditer(body):
            if (name := match.group(1).lower()) not in _IDENTITY_MACROS and name not in found:
                found.append(name)
        return body

    outside_literals(text, collect)
    return found


def state_macros(placement: str) -> frozenset[str]:
    """The macros a Situation template placed at *placement* may name."""
    return STATE_MACROS | AFTER_DIRECTOR_MACROS if placement == AFTER_DIRECTOR else STATE_MACROS


def macro_errors(text: str, *, allowed: frozenset[str] = STATE_MACROS, field: str = "Template") -> list[str]:
    errors = [
        f"{field} cannot put {{{{{name}}}}} inside {{{{random}}}}, {{{{pick}}}} or {{{{roll}}}}"
        for name in _nested_state_macros(text)
    ]
    for name, argument in dict.fromkeys(_macro_uses(text)):
        if name in allowed and _depth(name, argument) is None:
            reason = (
                f"the count must be a whole number from 1 to {_MAX_RECENT_HISTORY_DEPTH}"
                if name == "recent_history"
                else f"{{{{{name}}}}} takes no argument"
            )
            errors.append(f"{field} cannot use {{{{{name}::{argument}}}}}: {reason}")
    for name in macros_used(text):
        if name in allowed:
            continue
        if name in LATER_STAGE_MACROS:
            stage = LATER_STAGE_MACROS[name]
            fix = "; set Runs to After Director to use it" if name in AFTER_DIRECTOR_MACROS and name not in allowed else ""
            errors.append(f"{field} cannot use {{{{{name}}}}} before {stage}: it does not exist yet at this stage{fix}")
        else:
            errors.append(f"{field} does not support {{{{{name}}}}} (available: {', '.join(sorted(allowed))})")
    return errors


def definition_macro_errors(definition: DecisionDefinition) -> list[str]:
    """The renderer's full macro contract, shared by authoring and execution."""
    errors = macro_errors(definition.state_template, allowed=state_macros(definition.placement), field="Situation template")
    criteria = definition.criteria.values() if isinstance(definition.criteria, Mapping) else definition.criteria
    for texts, label in (
        ((definition.instructions,), "Question"),
        (criteria, "Outcome description"),
        (definition.outputs.values(), "Guidance"),
    ):
        for text in texts:
            errors.extend(macro_errors(text, allowed=TEXT_MACROS, field=label))
    return errors


class UnavailableMacro(Exception):
    def __init__(self, macro: str) -> None:
        super().__init__(macro)
        self.macro = macro


def render(
    template: str,
    snapshot: DecisionSnapshot,
    *,
    allowed: frozenset[str] = STATE_MACROS,
    seed: str = "",
    pick_seed: str | None = None,
) -> str:
    """Resolve names, then the template's inline macros, then insert values opaquely.

    Message text and the description go in last, so they are never evaluated.
    """

    def substitute(names: frozenset[str]):
        def one(match: re.Match) -> str:
            name = match.group(1).lower()
            depth = _depth(name, match.group(2))
            if name not in names or depth is None:
                return match.group(0)
            if (value := snapshot.value(name, depth)) is None:
                raise UnavailableMacro(name)
            return value

        return lambda body: _MACRO_RE.sub(one, body)

    identities = allowed & _IDENTITY_MACROS
    named = resolve_inline(outside_literals(template, substitute(identities)), seed=seed, pick_seed=pick_seed)
    return outside_literals(named, substitute(allowed - identities))
