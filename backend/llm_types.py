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

from typing import Literal, TypedDict, Union


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
    call -- assistant turns carrying ``tool_calls``, ``tool``-role results --
    are deliberately left as free-form ``dict`` (see the editor pass), mirroring
    the model layer's rule that only fixed-schema shapes get a contract.
    """

    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]
