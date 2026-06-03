"""LLM wire-format contracts: the shape of the OpenAI-style chat messages the
pipeline assembles and ships to the model.

Like ``backend/database/models.py`` (the data layer's contracts) and
``backend/workflows/contracts.py`` (the workflow layer's), this module is a
dependency-free leaf -- it describes a *shape* and imports nothing else in the
codebase, so every layer that builds or consumes messages (the prompt builder,
the three passes, the orchestrator, the summarizer) can point its dependency
inward at the contract rather than at the client implementation or the ``utils``
catch-all.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict, Union

from typing_extensions import NotRequired


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


# A message body is either a plain string or, for vision-capable turns, a list
# of typed parts. ``build_multimodal_content`` and
# ``format_message_with_attachments`` emit the list form.
ContentPart = Union[TextPart, ImagePart]


class ChatMessage(TypedDict):
    """One OpenAI-format chat message in a pipeline *prefix* (the system prompt
    plus chat history that every pass shares byte-for-byte for KV-cache reuse).

    A closed shape: a prefix only ever holds these three roles with text or
    multimodal content. The broader wire messages a pass *appends* before a
    call -- assistant turns carrying ``tool_calls`` / ``reasoning_content`` and
    ``tool``-role results -- are the other members of :data:`WireMessage`.
    """

    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]


class ToolCall(TypedDict):
    """An OpenAI-format tool call carried on an assistant wire message."""

    id: str
    type: Literal["function"]
    function: dict[str, Any]


class AssistantToolMessage(TypedDict):
    """An assistant turn that carries tool calls (and optional reasoning),
    appended by the ReAct loops when ``reasoning_on`` is set."""

    role: Literal["assistant"]
    content: str | list[ContentPart]
    tool_calls: list[ToolCall]
    reasoning_content: NotRequired[str]


class ToolResultMessage(TypedDict):
    """A ``tool``-role result turn answering a prior :class:`ToolCall`."""

    role: Literal["tool"]
    tool_call_id: str
    content: str


# The full mutable wire buffer a pass ships to the model: a ``ChatMessage``
# prefix plus the turns the ReAct loops append. Modelled as a union (not a
# single open TypedDict) because adding optional keys would make a superset
# TypedDict a *subtype* of ``ChatMessage`` -- the wrong direction -- so a
# ``ChatMessage`` could not flow into it. As a union member it flows in
# directly, letting a buffer be built ``[*prefix, ...]`` and typed
# ``list[WireMessage]`` with no cast.
WireMessage = Union[ChatMessage, AssistantToolMessage, ToolResultMessage]
