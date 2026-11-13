"""Contracts between the assistant loop and the tools it runs."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import cached_property
from typing import Annotated, Any, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, ConfigDict

from ...core import WireMessage

# Budgets for one run. Model calls include retries.
MAX_TOOL_EXECUTIONS = 12
MAX_MODEL_CALLS = 30
# The same tool with the same arguments, back to back: the model is not taking in the results.
MAX_SAME_CALLS = 3
# The longest request the browser may send.
MAX_ITEM_CHARS = 8000
# The conversation the model is sent, excluding the system prompt and tool schemas: every request, call and result so far.
# Orb has no setting for an endpoint's context window, so this is a fixed size, about 20,000 tokens.
CONTEXT_BUDGET_CHARS = 80_000
# How far under the budget the conversation is brought once it passes it. Dropping its oldest requests rewrites what the
# model already read and re-reads everything after, so it is done rarely and in bulk.
CONTEXT_SLACK_CHARS = CONTEXT_BUDGET_CHARS // 4
MAX_RESULT_CHARS = 6000


def encode_result(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


STEP_TOOL_NAME = "assistant_step"
REPLY = "reply"

# ``read`` runs at once and ``write`` commits at once. ``confirm`` writes nothing: it stores a proposal and the run carries on;
# the user reviews every proposal once the run ends, and their Apply commits outside the run. ``handoff`` writes nothing either:
# it checks a target and names it, the run ends, and the browser starts the work in that target's own stream, which owns it
# from then on.
ToolBehavior = Literal["read", "write", "confirm", "handoff"]

_JSON_TYPES: dict[Any, str] = {str: "string", int: "integer", bool: "boolean"}


def _flat_schema(annotation: Any) -> dict[str, Any]:
    """A field's provider schema: a scalar, a string enum, or a list of either. Anything else is a declaration error."""
    if get_origin(annotation) is Annotated:
        return _flat_schema(get_args(annotation)[0])
    if get_origin(annotation) is Literal:
        return {"type": "string", "enum": [str(value) for value in get_args(annotation)]}
    if get_origin(annotation) is list:
        item = _flat_schema(get_args(annotation)[0])
        if item["type"] == "array":
            raise TypeError("A tool argument may not nest lists")
        return {"type": "array", "items": item}
    return {"type": _JSON_TYPES[annotation]}


def _bounds(kind: str, metadata: Sequence[Any]) -> dict[str, int]:
    """A field's integer and list-size bounds as schema keywords; string lengths stay out of the schema."""
    found = {
        name: getattr(item, name)
        for item in metadata
        for name in ("ge", "gt", "le", "lt", "min_length", "max_length")
        if isinstance(getattr(item, name, None), int)
    }
    bounds: dict[str, int] = {}
    if kind == "integer":
        if "ge" in found or "gt" in found:
            bounds["minimum"] = found["ge"] if "ge" in found else found["gt"] + 1
        if "le" in found or "lt" in found:
            bounds["maximum"] = found["le"] if "le" in found else found["lt"] - 1
    elif kind == "array":
        if found.get("min_length"):
            bounds["minItems"] = found["min_length"]
        if "max_length" in found:
            bounds["maxItems"] = found["max_length"]
    return bounds


class ToolArguments(BaseModel):
    """Base for a tool's flat argument model: scalars, string enums and lists of them; unknown fields and coerced types refused."""

    model_config = ConfigDict(extra="forbid", strict=True)


class NoArguments(ToolArguments):
    """A tool that takes nothing; the loop skips its argument call."""


class ToolFailure(Exception):
    """An expected condition the model can act on: a missing target, an unavailable or busy resource."""


class AssistantUnavailable(RuntimeError):
    """The model gave no usable step, or the request cannot fit the working context."""


@dataclass(frozen=True)
class ToolOutput:
    """One tool result, already projected to its allowlist and bounded by the tool."""

    data: Mapping[str, Any]
    # One line for the browser's transcript.
    summary: str
    ids: Mapping[str, Any] = field(default_factory=dict)
    # For a write: the resources it committed, each ``{"kind": ..., <ids>}``. Returned only after the commit.
    committed: tuple[Mapping[str, Any], ...] = ()
    # For a confirm tool: the preview the browser shows with Apply and Cancel, naming the proposal that Apply commits. A later
    # preview with the same proposal id replaces the earlier one.
    proposal: Mapping[str, Any] | None = None
    # For a handoff tool: the checked target the browser passes to its own stream, ``{conversation_id, message_id, ...}``.
    handoff: Mapping[str, Any] | None = None
    # A write after which the rest of the request would read a context that has changed under it: the run ends with it.
    ends_run: bool = False


def _field_schema(annotation: Any, metadata: Sequence[Any], description: str | None) -> dict[str, Any]:
    schema = _flat_schema(annotation)
    return {**schema, **_bounds(schema["type"], metadata), **({"description": description} if description else {})}


@dataclass(frozen=True)
class AssistantTool:
    name: str
    description: str
    arguments: type[ToolArguments]
    behavior: ToolBehavior
    run: Callable[[Any], Awaitable[ToolOutput]]
    # A write that sets an explicit state, so running it again changes nothing: exempt from the rule that a change runs once.
    idempotent: bool = False
    # A tool that may run for minutes and reports progress labels to the run's progress queue while it does.
    reports_progress: bool = False

    @cached_property
    def parameters(self) -> dict[str, Any]:
        """The flat provider schema for ``arguments``."""
        fields = self.arguments.model_fields
        properties = {name: _field_schema(info.annotation, info.metadata, info.description) for name, info in fields.items()}
        return {
            "type": "object",
            "properties": properties,
            "required": [name for name, info in fields.items() if info.is_required()],
            "additionalProperties": False,
        }

    @property
    def takes_arguments(self) -> bool:
        return bool(self.arguments.model_fields)


_Run = TypeVar("_Run", bound=Callable[[Any], Awaitable[ToolOutput]])


class ToolSet(list[AssistantTool]):
    """Tools in the order they are declared, each by decorating its callable; the name defaults to the callable's."""

    def add(
        self, behavior: ToolBehavior, arguments: type[ToolArguments], description: str, *, name: str = "", **options: Any
    ) -> Callable[[_Run], _Run]:
        def declare(run: _Run) -> _Run:
            self.append(AssistantTool(name or run.__name__, description, arguments, behavior, run, **options))
            return run

        return declare


@dataclass(frozen=True)
class ViewContext:
    """The view a request was sent from, with its ids already validated by the caller."""

    mode: Literal["chat", "document", "library"]
    conversation_id: str | None = None
    conversation_title: str = ""
    group: bool = False
    character_id: str | None = None
    character_name: str = ""
    document_id: str | None = None
    document_title: str = ""
    # The world open in the lorebook drawer, and the entry selected there; either may accompany any mode.
    world_id: str | None = None
    world_name: str = ""
    entry_id: int | None = None
    entry_name: str = ""


@dataclass(frozen=True)
class Transcript:
    """The assistant's conversation so far, as the model was sent it: every request, call and result, in order.

    *model* and *endpoint* made it. Reasoning one provider signed is not valid on another, so a different pair replays it
    without its reasoning.
    """

    messages: tuple[WireMessage, ...]
    model: str
    endpoint: str


# One stream event: ``{"type": ..., **payload}``, as listed under Events in docs/architecture/assistant-panel.md.
AssistantEvent = dict[str, Any]
