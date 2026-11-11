"""What every assistant tool shares: bounded ids and labels, and the checks that resolve a tool's target from its arguments
and the view the request was sent from."""

from __future__ import annotations

import json
from typing import Any, Literal, get_args

from pydantic import Field

from ...database import get_active_path, get_conversation, get_message_by_id, resolve_cast
from ...database.models import ConversationRow, MessageRow
from ...features.assistant import ToolFailure, ViewContext
from ..deps import conversation_busy, resource_deleting

# How long an id argument to any assistant tool may be.
ID_CHARS = 64
_LABEL_CHARS = 120
# The authored card text a reader may see and an edit may change. Prompt overrides, extensions, avatars and workflow state
# stay out.
CharacterField = Literal["description", "personality", "scenario", "first_mes", "mes_example", "creator_notes"]
CHARACTER_FIELDS: tuple[CharacterField, ...] = get_args(CharacterField)
# A card's list fields, which a read shows and an edit proposes whole.
CharacterListField = Literal["tags", "alternate_greetings"]
CHARACTER_LIST_FIELDS: tuple[CharacterListField, ...] = get_args(CharacterListField)


def chat_field() -> Any:
    return Field(default="", max_length=ID_CHARS)


def clip(value: object, cap: int) -> str:
    """*value* as text, shortened to *cap* characters with a marker naming how many were left out."""
    text = str(value or "")
    return f"{text[:cap]}…[+{len(text) - cap} chars]" if len(text) > cap else text


def quoted(text: object) -> str:
    """Authored text as a short JSON-quoted label, so a name reads as data inside a sentence."""
    value = str(text or "")
    return json.dumps(value[:_LABEL_CHARS] + ("…" if len(value) > _LABEL_CHARS else ""), ensure_ascii=False)


def not_blank(value: str | list[str]) -> str | list[str]:
    """A field validator for every assistant tool: text, or any item of a list of texts, must not be only whitespace."""
    if any(not text.strip() for text in ([value] if isinstance(value, str) else value)):
        raise ValueError("must not be blank")
    return value


def chat_id(conversation_id: str, view: ViewContext) -> str:
    """The chat a tool names, else the one in view."""
    cid = conversation_id or view.conversation_id
    if not cid:
        raise ToolFailure("No chat is in view. Name a conversation_id; query_library can find one.")
    return cid


async def chat_in_view(conversation_id: str, view: ViewContext, *, changes: bool) -> ConversationRow:
    """The chat a tool names, else the one in view, read now.

    *changes* is for a tool that changes the chat or starts work in it: a chat being deleted is refused to it, since what it
    made would go with the chat. A read still sees that chat, which is stored until the delete ends, and changes nothing.
    """
    cid = chat_id(conversation_id, view)
    conv = await get_conversation(cid)
    if conv is None:
        raise ToolFailure(f"No chat has the id {cid}.")
    if changes and resource_deleting(cid):
        raise ToolFailure("That chat is being deleted, so nothing was done.")
    return conv


async def cast_of(conv: ConversationRow) -> list[dict[str, Any]]:
    """A chat's cast as a tool shows it: each member's name, character id and, in a group, member id; and whether it is muted."""
    cast = await resolve_cast(conv)
    return [
        {
            "name": member.name,
            "character_id": member.card_id,
            **({"member_id": member.member_id} if cast.grouped else {}),
            **({"muted": True} if member.muted else {}),
        }
        for member in cast.members
    ]


async def character_in_view(character_id: str, view: ViewContext) -> str:
    """The character a tool names, else the one in view; a group view infers none and lists its cast instead."""
    card_id = character_id or view.character_id
    if card_id:
        return card_id
    if view.group and view.conversation_id:
        conv = await get_conversation(view.conversation_id)
        cast = await cast_of(conv) if conv else []
        raise ToolFailure(f"This is a group chat. Name one character_id from its cast: {json.dumps(cast)}")
    raise ToolFailure("No character is in view. Name a character_id; list_characters can find one.")


async def reply_target(
    conversation_id: str, message_id: int, view: ViewContext, what: str, *, idle: bool
) -> tuple[ConversationRow, MessageRow]:
    """The chat, and the saved reply on its kept branch, that a handoff or a media tool names.

    *what* completes "only a reply can be …". *idle* also refuses a chat that is generating, as a handoff must: the chat's own
    stream would refuse to start.
    """
    conv = await chat_in_view(conversation_id, view, changes=True)
    cid = conv["id"]
    if idle and conversation_busy(cid):
        raise ToolFailure("That chat is generating right now. Ask again once it has finished.")
    message = await get_message_by_id(message_id)
    if message is None or message["conversation_id"] != cid:
        raise ToolFailure(f"That chat has no message {message_id}. read_messages shows the ids.")
    if message["role"] != "assistant":
        raise ToolFailure(f"Message {message['id']} is the user's own message; only a reply can be {what}.")
    if not any(row["id"] == message["id"] for row in await get_active_path(cid)):
        raise ToolFailure(
            f"Message {message['id']} is on a branch the chat is not showing, so it cannot be {what} there. "
            "switch_branch shows that branch first, if that is what the user wants."
        )
    return conv, message
