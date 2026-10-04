"""Wire-format contracts for LLM chat messages."""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict


class TextPart(TypedDict):
    """A text content part in a multimodal message body."""

    type: Literal["text"]
    text: str


class ImageURLSpec(TypedDict):
    """The ``image_url`` payload of an :class:`ImagePart` (a ``data:`` URL)."""

    url: str


class ImagePart(TypedDict):
    """An image content part in a multimodal message body."""

    type: Literal["image_url"]
    image_url: ImageURLSpec


# A message body is either a plain string or, for vision-capable turns, a list of typed parts. ``build_multimodal_content`` and
# ``format_message_with_attachments`` emit the list form.
ContentPart = TextPart | ImagePart


class ChatMessage(TypedDict):
    """A text or multimodal message in the shared, byte-stable pipeline prefix.

    Pass-appended tool calls, reasoning and tool results use WireMessage instead.
    """

    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]


class ToolFunction(TypedDict):
    """A wire tool call's name and still-encoded JSON arguments."""

    name: str
    arguments: str


class ToolCall(TypedDict):
    """An OpenAI-format tool call carried on an assistant wire message."""

    id: str
    type: Literal["function"]
    function: ToolFunction


class ReasoningReplay(TypedDict, total=False):
    """A model's reasoning as replayed on its assistant turn, under the field
    names the provider streamed it with (see ``inference.replay_reasoning``)."""

    reasoning_content: str
    reasoning: str
    reasoning_details: list[dict[str, Any]]


class CompletionMessage(ReasoningReplay, total=False):
    """An assembled model reply; absent fields stay absent on the wire."""

    content: str
    tool_calls: list[ToolCall]
    finish_reason: str


class ContentDelta(TypedDict):
    type: Literal["content"]
    delta: str


class ReasoningDelta(TypedDict):
    type: Literal["reasoning"]
    delta: str
    call_start: NotRequired[bool]


class TokenAlternative(TypedDict):
    t: str
    p: float


class TokenProbability(TypedDict):
    token: str
    prob: float
    top: list[TokenAlternative]


class TokenProbsEvent(TokenProbability):
    type: Literal["token_probs"]


class CompletionDone(TypedDict):
    type: Literal["done"]
    message: CompletionMessage
    # Providers carry different usage extensions; preserve their JSON verbatim.
    usage: dict[str, Any] | None


CompletionDelta = ContentDelta | ReasoningDelta | TokenProbsEvent
CompletionEvent = CompletionDelta | CompletionDone


class ParsedToolCall(TypedDict):
    """A normalized call after parsing; tool-owned arguments stay open."""

    name: str
    arguments: dict[str, Any]


class AssistantToolMessage(ReasoningReplay):
    """An assistant turn that carries tool calls (and optional reasoning),
    appended by the ReAct loops when ``reasoning_on`` is set."""

    role: Literal["assistant"]
    content: str | list[ContentPart]
    tool_calls: list[ToolCall]


class ToolResultMessage(TypedDict):
    """A ``tool``-role result turn answering a prior :class:`ToolCall`."""

    role: Literal["tool"]
    tool_call_id: str
    content: str


# The full mutable wire buffer a pass ships to the model: a ``ChatMessage`` prefix plus the turns the ReAct loops append.
# Modelled as a union (not a single open TypedDict) because adding optional keys would make a superset TypedDict a *subtype* of
# ``ChatMessage`` -- the wrong direction -- so a ``ChatMessage`` could not flow into it. As a union member it flows in directly,
# letting a buffer be built ``[*prefix, ...]`` and typed ``list[WireMessage]`` with no cast.
WireMessage = ChatMessage | AssistantToolMessage | ToolResultMessage
