"""The decision-scoped renderer: a frozen turn snapshot and a small macro set.

Two rules shape everything here.

**The renderer sees a snapshot, not the pipeline.** It receives explicit turn
data and nothing else -- no ``PipelineContext``, no filesystem, no database, no
hidden member sheet. ``core/macros.py`` is deliberately not given access to any
of that either; this module owns its own short macro list instead, which is why
the list can be validated both when an author saves and again at execution time.

**Authored template tokens resolve once, and message bodies go in as opaque
values.** A ``{{char}}`` a *character wrote in a reply* is not a macro the
classifier input should expand: the substitution is a single pass whose
replacements are never rescanned, so prose that happens to look like a macro
reaches the classifier exactly as the story contains it.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ....core import Macros, outside_literals
from ....inference import MAX_QUESTION_BYTES, MAX_STATE_BYTES
from ....prompting import format_message_with_attachments, group_speaker_label

#: The renderer's contract version. Part of the resolution-policy fingerprint,
#: so changing what a macro means creates a new occurrence on the next
#: regeneration instead of silently replaying an answer to a different question.
DECISION_RENDERER_VERSION = "1"

#: How many completed messages ``{{recent_history}}`` carries.
RECENT_HISTORY_DEPTH = 4

#: Macros the state template may use, and what each means before the Director.
STATE_MACROS: frozenset[str] = frozenset(
    {"last_message", "last_assistant_message", "recent_history", "user", "char", "cast", "description"}
)

#: Macros the authored question, criteria and output text may use. Narrower on
#: purpose: those fields are also cache-key and fingerprint inputs, so a macro
#: that resolved differently per turn there would fragment the cache and make
#: two identical questions look like two questions.
TEXT_MACROS: frozenset[str] = frozenset({"user", "char", "cast"})

#: Macros that belong to a later stage. Named separately so the editor can say
#: *why* they are unavailable rather than "unknown macro".
LATER_STAGE_MACROS: frozenset[str] = frozenset({"scene_guidance", "draft"})

_MACRO_RE = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class DecisionSnapshot:
    """One stage's frozen input. Every decision at a stage reads this same object.

    That sharing is the invariant, not an optimization: results from a stage
    cannot affect another decision in the same stage, so there is exactly one
    snapshot per stage and no decision can observe a sibling's outcome.

    ``description`` is ``None`` when the scope has no single character to read it
    off -- the group exchange stage. ``None`` is not ``""``: a decision whose
    template needs it falls back with ``unavailable_context`` rather than
    rendering an empty description and answering a question about nobody.
    """

    last_message: str = ""
    last_assistant_message: str = ""
    recent_history: str = ""
    user: str = ""
    char: str = ""
    cast: str = ""
    description: str | None = None
    #: ``solo`` or ``group``; a before-Director group decision is scene-wide.
    scope: str = "solo"
    #: The message the classifier input was read from, for replay and for
    #: identifying the original exchange input on a later-speaker regeneration.
    anchor_message_id: int | None = None

    def value(self, macro: str) -> str | None:
        """The snapshot value for *macro*; ``None`` means unavailable here."""
        if macro == "description":
            return self.description
        return getattr(self, macro, None)


def _message_text(message: Mapping[str, Any], macros: Macros | None, scripts: Any = None) -> str:
    """One history row as prompt-channel text, attachment bytes dropped.

    The same projection the prompt uses -- identity macros, then the card's
    prompt-channel scripts, then textual attachment annotations -- so the
    classifier reads what the writer reads. Image parts are dropped rather than
    described: a decision is a text question, and a decision that silently
    stopped seeing the picture is better than one that ships megabytes of base64
    to a classifier.
    """
    rendered = format_message_with_attachments(message, macros, scripts)
    content = rendered["content"]
    if isinstance(content, str):
        return content
    # Narrowed on the discriminant rather than filtered by it, so the image parts
    # are dropped by the type system and not merely by convention.
    return "\n\n".join(part["text"] for part in content if part["type"] == "text")


def _labelled(message: Mapping[str, Any], text: str, speaker_names: Mapping[str, str], grouped: bool) -> str:
    """A history line, speaker-attributed in a group and role-tagged otherwise."""
    if message.get("role") != "assistant":
        return f"User: {text}"
    if grouped:
        return f"{group_speaker_label(speaker_names, message.get('speaker_member_id'))}: {text}"
    return f"Assistant: {text}"


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
    """Freeze one stage's classifier input from explicit turn data.

    *current_request* is the message the Writer will actually receive, so
    steering supplied by Magic Rewrite or super-regenerate is part of it -- and
    therefore part of the state, the fingerprint, and the classifier's view. It
    must not disappear: a decision that judged the unsteered request would be
    answering a question the user already replaced.

    *history* is the input branch, excluding the current request. It is read from
    the end, so the caller passes the whole branch and pays only for the depth
    the macros use.
    """
    grouped = scope == "group"
    names = dict(speaker_names or {})
    scoped = speaker_scripts or {}

    def _scripts_for(message: Mapping[str, Any]) -> Any:
        return scoped.get(str(message.get("speaker_member_id"))) if grouped else scripts

    completed = [message for message in history if message.get("role") in ("user", "assistant")]
    last_assistant = next((message for message in reversed(completed) if message.get("role") == "assistant"), None)
    tail = completed[-RECENT_HISTORY_DEPTH:]

    previous_reply = ""
    if last_assistant is not None:
        body = _message_text(last_assistant, macros, _scripts_for(last_assistant))
        # A speaker label only where it disambiguates. In a solo chat there is
        # one voice, and prefixing it would put a word in the state that the
        # default template's own "Previous reply:" heading already supplies.
        previous_reply = f"{group_speaker_label(names, last_assistant.get('speaker_member_id'))}: {body}" if grouped else body

    return DecisionSnapshot(
        last_message=current_request,
        last_assistant_message=previous_reply,
        recent_history="\n\n".join(
            _labelled(message, _message_text(message, macros, _scripts_for(message)), names, grouped) for message in tail
        ),
        user=macros.user,
        char=macros.char,
        cast=macros.cast,
        description=description,
        scope=scope,
        anchor_message_id=anchor_message_id,
    )


def macros_used(text: str) -> list[str]:
    """Every macro name in *text*, lowercased, backticked spans excluded.

    Order-preserving and de-duplicated, so an error message names the offending
    macros in the order the author wrote them.
    """
    found: list[str] = []

    def _collect(body: str) -> str:
        for match in _MACRO_RE.finditer(body):
            name = match.group(1).lower()
            if name not in found:
                found.append(name)
        return body

    outside_literals(text, _collect)
    return found


def macro_errors(text: str, *, allowed: frozenset[str] = STATE_MACROS, field: str = "Template") -> list[str]:
    """Every unsupported macro in *text*, as author-facing prose.

    Separate from the emptiness check because the two do not always travel
    together: an authored output may legitimately be empty (that is how an author
    says "nothing to add for this outcome") but may still not invent a macro.
    """
    errors: list[str] = []
    for name in macros_used(text):
        if name in allowed:
            continue
        if name in LATER_STAGE_MACROS:
            errors.append(f"{field} cannot use {{{{{name}}}}} before the Director: it does not exist yet at this stage")
        else:
            errors.append(f"{field} does not support {{{{{name}}}}} (available: {', '.join(sorted(allowed))})")
    return errors


def template_errors(template: str, *, allowed: frozenset[str] = STATE_MACROS, field: str = "Template") -> list[str]:
    """Why *template* is not renderable at this stage, in author-facing prose.

    Validated when the author saves *and* again before the request goes out: a
    template can become invalid without being edited -- a solo fragment moved
    into a group loses ``{{description}}`` -- and only the execution-time check
    sees that.
    """
    if not template.strip():
        return [f"{field} must not be empty"]
    return macro_errors(template, allowed=allowed, field=field)


class UnavailableMacro(Exception):
    """A supported macro whose value this scope cannot supply.

    Raised, not defaulted: a fragment written for a solo chat and read in a group
    has no ``{{description}}``, and inferring an owning or speaking member for it
    would answer a question about a character nobody selected.
    """

    def __init__(self, macro: str) -> None:
        super().__init__(macro)
        self.macro = macro


def render(template: str, snapshot: DecisionSnapshot, *, allowed: frozenset[str] = STATE_MACROS) -> str:
    """Substitute *template*'s macros from *snapshot*, once.

    A single pass whose replacements are never rescanned, so a message body that
    contains ``{{char}}`` -- or a nested ``{{random}}`` -- reaches the classifier
    verbatim. An unknown macro is left raw: it survived validation only if the
    caller skipped it, and blanking an author's text is worse than showing it.
    """

    def _fire(body: str) -> str:
        def _one(match: re.Match) -> str:
            name = match.group(1).lower()
            if name not in allowed:
                return match.group(0)
            value = snapshot.value(name)
            if value is None:
                raise UnavailableMacro(name)
            return value

        return _MACRO_RE.sub(_one, body)

    return outside_literals(template, _fire)


def oversized_state(state: str) -> bool:
    """Whether a rendered state exceeds Orb's conservative input limit."""
    return len(state.encode("utf-8")) > MAX_STATE_BYTES


def oversized_question(instructions: str, criteria: Mapping[str, str]) -> bool:
    """Whether one rendered question, criteria included, exceeds the limit."""
    total = len(instructions.encode("utf-8")) + sum(len(text.encode("utf-8")) for text in criteria.values())
    return total > MAX_QUESTION_BYTES


__all__ = [
    "DECISION_RENDERER_VERSION",
    "LATER_STAGE_MACROS",
    "RECENT_HISTORY_DEPTH",
    "STATE_MACROS",
    "TEXT_MACROS",
    "DecisionSnapshot",
    "UnavailableMacro",
    "build_snapshot",
    "macros_used",
    "oversized_question",
    "oversized_state",
    "render",
    "template_errors",
]
