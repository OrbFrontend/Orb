"""Contracts between the assistant loop and the tools its caller supplies."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from typing import Annotated, Any, Literal, TypeVar, get_args, get_origin

from pydantic import BaseModel, ConfigDict

# Budgets for one run. Model calls include retries.
MAX_TOOL_EXECUTIONS = 12
MAX_MODEL_CALLS = 30
# Bounds on what a browser may replay and on the working context built from it.
MAX_REPLAY_ITEMS = 100
MAX_ITEM_CHARS = 8000
# A user entry's view label, as the browser wrote it.
MAX_VIEW_LABEL_CHARS = 200
CONTEXT_BUDGET_CHARS = 20_000
MAX_RESULT_CHARS = 6000

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
    # One line for the transcript, and what remains of the result once its body is dropped to save context.
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
        properties = {
            name: {**_flat_schema(info.annotation), **({"description": info.description} if info.description else {})}
            for name, info in fields.items()
        }
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
class HistoryItem:
    """One replayed transcript entry. It comes from the browser, so it is a record to quote, never proof of an action."""

    kind: Literal["user", "assistant", "tool", "outcome"]
    text: str
    tool: str = ""
    # For a user entry: the view it was sent from, as the browser labelled it.
    view: str = ""


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


# One stream event: ``{"type": ..., **payload}``, as listed under Events in docs/architecture/assistant-panel.md.
AssistantEvent = dict[str, Any]
