from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from ....core import (
    CastMember,
    DecisionDefinition,
    Macros,
    outside_literals,
    resolve_inline,
)
from ....prompting import format_message_with_attachments, group_speaker_label

DECISION_RENDERER_VERSION = "1"
RECENT_HISTORY_DEPTH = 6
STATE_MACROS = frozenset({"last_message", "last_assistant_message", "recent_history", "user", "char", "cast", "description"})
TEXT_MACROS = frozenset({"user", "char", "cast"})
# Names, resolved before inline macros as the main prompt path does, so they can
# sit inside a {{random}} option. Message bodies are inserted after, and never can.
_IDENTITY_MACROS = TEXT_MACROS
LATER_STAGE_MACROS = frozenset({"scene_guidance", "draft"})
# Inline macros resolve in every decision field; listed here as authors write them.
INLINE_MACROS = ("roll::1d20", "random::a::b", "pick::a::b", "time", "date", "trim", "// note")
_MACRO_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}", re.IGNORECASE)
_NESTED_RE = re.compile(r"\{\{(?:random|pick|roll)::[^{}]*\{\{\s*([a-z_]+)\s*\}\}", re.IGNORECASE)
# Validation resolves inline macros only to remove them; any fixed seed will do.
_VALIDATION_SEED = "validate"


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    last_message: str = ""
    last_assistant_message: str = ""
    recent_history: str = ""
    user: str = ""
    char: str = ""
    cast: str = ""
    description: str | None = None
    scope: str = "solo"
    anchor_message_id: int | None = None
    seed: str = ""

    def value(self, macro: str) -> str | None:
        return self.description if macro == "description" else getattr(self, macro, None)

    def roll_seed(self, fragment_id: str | None, field: str) -> str:
        """Seed one field's inline macros: fixed within an exchange, fresh across exchanges.

        A regeneration renders the same bytes, so replay and the answer cache still
        match. ``None`` shares the seed across fragments -- the Situation's, so one
        exchange shows every question the same world and equal templates still
        batch. No seed means fresh rolls.
        """
        return f"{self.seed}|{self.anchor_message_id}|{fragment_id or ''}|{field}" if self.seed else ""


def _resolved_description(macros: Macros, description: str | None) -> str | None:
    """Resolve *description* the way the Writer's ``{{description}}`` does."""
    return macros._replace(description=description).resolve_message("{{description}}") if description else description


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
    anchor_message_id: int | None = None,
) -> DecisionSnapshot:
    grouped = scope == "group"
    names = speaker_names or {}
    scoped_scripts = speaker_scripts or {}

    def scripts_for(message: Mapping[str, Any]) -> Any:
        return scoped_scripts.get(str(message.get("speaker_member_id"))) if grouped else scripts

    completed = [message for message in history if message.get("role") in ("user", "assistant")]
    last_assistant = next((message for message in reversed(completed) if message.get("role") == "assistant"), None)
    previous_reply = ""
    if last_assistant:
        body = _message_text(last_assistant, macros, scripts_for(last_assistant))
        previous_reply = f"{group_speaker_label(names, last_assistant.get('speaker_member_id'))}: {body}" if grouped else body
    return DecisionSnapshot(
        last_message=current_request,
        last_assistant_message=previous_reply,
        recent_history="\n\n".join(
            _labelled(message, _message_text(message, macros, scripts_for(message)), names, grouped)
            for message in completed[-RECENT_HISTORY_DEPTH:]
        ),
        user=macros.user,
        char=macros.char,
        cast=macros.cast,
        description=_resolved_description(macros, description),
        scope=scope,
        anchor_message_id=anchor_message_id,
        seed=macros.seed,
    )


def card_snapshots(snapshot: DecisionSnapshot, members: Iterable[CastMember]) -> dict[str, DecisionSnapshot]:
    """Scope card-embedded decisions to each card's first cast member."""
    scoped: dict[str, DecisionSnapshot] = {}
    for member in members:
        if member.card_id and member.card_id not in scoped:
            member_macros = Macros(user=snapshot.user, char=member.name, seed=snapshot.seed, cast=snapshot.cast)
            description = _resolved_description(member_macros, member.private_sheet)
            scoped[member.card_id] = replace(snapshot, char=member.name, description=description)
    return scoped


def macros_used(text: str) -> list[str]:
    found: list[str] = []

    def collect(body: str) -> str:
        for match in _MACRO_RE.finditer(body):
            if (name := match.group(1).lower()) not in found:
                found.append(name)
        return body

    outside_literals(resolve_inline(text, seed=_VALIDATION_SEED), collect)
    return found


def _nested_state_macros(text: str) -> list[str]:
    found: list[str] = []

    def collect(body: str) -> str:
        for match in _NESTED_RE.finditer(body):
            if (name := match.group(1).lower()) not in _IDENTITY_MACROS and name not in found:
                found.append(name)
        return body

    outside_literals(text, collect)
    return found


def macro_errors(text: str, *, allowed: frozenset[str] = STATE_MACROS, field: str = "Template") -> list[str]:
    errors = [
        f"{field} cannot put {{{{{name}}}}} inside {{{{random}}}}, {{{{pick}}}} or {{{{roll}}}}"
        for name in _nested_state_macros(text)
    ]
    for name in macros_used(text):
        if name in allowed:
            continue
        if name in LATER_STAGE_MACROS:
            errors.append(f"{field} cannot use {{{{{name}}}}} before the Director: it does not exist yet at this stage")
        else:
            errors.append(f"{field} does not support {{{{{name}}}}} (available: {', '.join(sorted(allowed))})")
    return errors


def definition_macro_errors(definition: DecisionDefinition) -> list[str]:
    """The renderer's full macro contract, shared by authoring and execution."""
    errors = macro_errors(definition.state_template, field="Situation template")
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


def render(template: str, snapshot: DecisionSnapshot, *, allowed: frozenset[str] = STATE_MACROS, seed: str = "") -> str:
    """Resolve names, then the template's inline macros, then insert values opaquely.

    Message text and the description go in last, so they are never evaluated.
    """

    def substitute(names: frozenset[str]):
        def one(match: re.Match) -> str:
            name = match.group(1).lower()
            if name not in names:
                return match.group(0)
            if (value := snapshot.value(name)) is None:
                raise UnavailableMacro(name)
            return value

        return lambda body: _MACRO_RE.sub(one, body)

    identities = allowed & _IDENTITY_MACROS
    named = resolve_inline(outside_literals(template, substitute(identities)), seed=seed)
    return outside_literals(named, substitute(allowed - identities))
