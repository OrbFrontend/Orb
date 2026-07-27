"""The fixed host effect envelope an extension invocation returns.

Community packages cannot invent SSE event names or frontend callbacks. Every
interpreter output is translated into this envelope, and the *frontend* owns
the mapping from an effect to a refetch. That inversion is the point: a package
says "conversation messages changed", never "call this function" or "fetch this
URL", so a package string never becomes an event name, a DOM selector, a
function name, or a module path.

The resource vocabulary is closed and small. An unknown effect is dropped and
logged rather than dispatched -- if a future Orb adds a resource, a package
built for it degrades to "no repaint" on an older build instead of reaching
an unintended handler.

``runtime_generation`` rides along on every envelope so the frontend can
discard a response computed against a catalog it has already replaced.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, get_args

from pydantic import Field

from .common import ExtensionId, ExtModel, LocalId


class MessagesEffect(ExtModel):
    """The conversation's active message path changed."""

    resource: Literal["conversation.messages"]
    conversation_id: str


class DirectorEffect(ExtModel):
    """The conversation's Director state changed."""

    resource: Literal["conversation.director"]
    conversation_id: str


class DirectionNotesEffect(ExtModel):
    """The conversation's direction notes changed."""

    resource: Literal["conversation.direction_notes"]
    conversation_id: str


class CharacterCardEffect(ExtModel):
    """One character card changed -- currently only its tag list.

    Emitted once per invocation, which for a renderer-driven library sweep
    means once per card. That is deliberate: an effect describes what *one
    invocation* did, and coalescing 300 of them into a bounded number of
    refetches is the frontend's business. Asking the host to emit fewer would
    make the envelope lie about what happened.
    """

    resource: Literal["character.card"]
    card_id: str


class ExtensionViewEffect(ExtModel):
    """One extension view should refetch its data and repaint."""

    resource: Literal["extension.view"]
    extension_id: ExtensionId
    view: LocalId


class ExtensionCatalogEffect(ExtModel):
    """The installed-extension catalog changed (install/enable/permissions)."""

    resource: Literal["extension.catalog"]


class Toast(ExtModel):
    """One host-rendered notification staged by ``ui.toast``."""

    text: str = Field(min_length=1, max_length=200)
    tone: Literal["info", "success", "warning", "error"] = "info"


Effect = Annotated[
    MessagesEffect | DirectorEffect | DirectionNotesEffect | CharacterCardEffect | ExtensionViewEffect | ExtensionCatalogEffect,
    Field(discriminator="resource"),
]

EFFECT_RESOURCES: frozenset[str] = frozenset(
    get_args(model.model_fields["resource"].annotation)[0] for model in get_args(get_args(Effect)[0])
)


class EffectEnvelope(ExtModel):
    """What every extension action, view, and lifecycle route returns."""

    data: Any = None
    effects: list[Effect] = Field(default_factory=list, max_length=32)
    toasts: list[Toast] = Field(default_factory=list, max_length=128)
    runtime_generation: int = Field(ge=0)
