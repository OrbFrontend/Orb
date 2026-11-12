"""What every assistant tool shares: bounded ids and labels, and the checks that resolve a tool's target from its arguments
and the view the request was sent from."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal, get_args

from pydantic import Field

from ...database import (
    get_active_path,
    get_character_card,
    get_conversation,
    get_interactive_fragment,
    get_lorebook_entry,
    get_message_by_id,
    get_mood_fragment,
    get_user_persona,
    get_world,
    resolve_cast,
)
from ...database.models import (
    CharacterCardRow,
    ConversationRow,
    InteractiveFragmentRow,
    LorebookEntryRow,
    MessageRow,
    MoodFragmentRow,
    UserPersonaRow,
    WorldRow,
)
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
    return Field(default="", max_length=ID_CHARS, description="Empty: the chat in view.")


def character_field() -> Any:
    return Field(default="", max_length=ID_CHARS, description="Empty: the character in view.")


def world_field() -> Any:
    return Field(default="", max_length=ID_CHARS, description="Empty: the world open in the lorebook. Ids: list_worlds.")


def entry_field() -> Any:
    return Field(default=0, ge=0, description="0: the entry selected in the lorebook. Ids: list_lore_entries.")


def fragment_field() -> Any:
    return Field(min_length=1, max_length=ID_CHARS, description="The fragment's id, from list_fragments.")


def open_in_view(conv: ConversationRow, view: ViewContext) -> None:
    """Refuse a handoff the browser could not start: the chat's stream runs only in the chat open in the main view."""
    if view.mode == "document" or (view.conversation_id and conv["id"] != view.conversation_id):
        raise ToolFailure(
            "That chat is not the one open in the main view, so nothing can start there. open_chat opens it if the user "
            "asks; they then ask again."
        )


def clip(value: object, cap: int) -> str:
    """*value* as text, shortened to *cap* characters with a marker naming how many were left out."""
    text = str(value or "")
    return f"{text[:cap]}…[+{len(text) - cap} chars]" if len(text) > cap else text


def quoted(text: object) -> str:
    """Authored text as a short JSON-quoted label, so a name reads as data inside a sentence."""
    value = str(text or "")
    return json.dumps(value[:_LABEL_CHARS] + ("…" if len(value) > _LABEL_CHARS else ""), ensure_ascii=False)


def message_label(path: Sequence[Mapping[str, Any]], message_id: int) -> str:
    """A message as the chat window numbers it: ``#N`` counted from 1 along the kept branch *path*."""
    index = next((i for i, row in enumerate(path) if row["id"] == message_id), None)
    return f"message #{index + 1}" if index is not None else "a message off the kept branch"


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


async def chat_in_view(conversation_id: str, view: ViewContext, *, changes: bool, idle: bool = False) -> ConversationRow:
    """The chat a tool names, else the one in view, read now.

    *changes* is for a tool that changes the chat or starts work in it: a chat being deleted is refused to it, since what it
    made would go with the chat. A read still sees that chat, which is stored until the delete ends, and changes nothing.
    *idle* also refuses a chat that is generating.
    """
    cid = chat_id(conversation_id, view)
    conv = await get_conversation(cid)
    if conv is None:
        raise ToolFailure(f"No chat has the id {cid}.")
    if changes and resource_deleting(cid):
        raise ToolFailure("That chat is being deleted, so nothing was done.")
    if idle and conversation_busy(cid):
        raise ToolFailure("That chat is generating right now. Ask again once it has finished.")
    return conv


async def message_of(cid: str, message_id: int) -> MessageRow:
    message = await get_message_by_id(message_id)
    if message is None or message["conversation_id"] != cid:
        raise ToolFailure(f"That chat has no message {message_id}. read_messages or query_library shows the ids.")
    return message


async def world_of(world_id: str, view: ViewContext) -> WorldRow:
    """The world a tool names, else the one open in the lorebook drawer."""
    wid = world_id or view.world_id
    if not wid:
        raise ToolFailure("No world is open in the lorebook. Name a world_id; list_worlds shows the ids.")
    world = await get_world(wid)
    if world is None:
        raise ToolFailure(f"No world has the id {wid}. list_worlds shows the ids.")
    return world


async def entry_of(world_id: str, entry_id: int, view: ViewContext) -> tuple[WorldRow, LorebookEntryRow]:
    """The lore entry a tool names, else the one selected in the lorebook drawer, and the world it belongs to.

    An entry named without a world is found in its own world; a world named with it must be that one.
    """
    if not entry_id and not view.entry_id:
        raise ToolFailure("No lore entry is selected in the lorebook. Name an entry_id; list_lore_entries shows the ids.")
    eid = entry_id or view.entry_id
    entry = await get_lorebook_entry(eid) if eid else None
    if entry is None:
        raise ToolFailure(f"No lore entry has the id {eid}. list_lore_entries shows the ids.")
    world = await world_of(world_id or entry["world_id"], view)
    if entry["world_id"] != world["id"]:
        raise ToolFailure(f"That world has no entry {eid}. list_lore_entries shows its entries.")
    return world, entry


async def persona_of(persona_id: int) -> UserPersonaRow:
    persona = await get_user_persona(persona_id)
    if persona is None:
        raise ToolFailure(f"No persona has the id {persona_id}. list_personas shows the ids.")
    return persona


async def mood_fragment_of(fragment_id: str) -> MoodFragmentRow:
    fragment = await get_mood_fragment(fragment_id)
    if fragment is None:
        raise ToolFailure(f"No mood fragment has the id {fragment_id}. list_fragments shows the ids.")
    return fragment


async def interactive_fragment_of(fragment_id: str) -> InteractiveFragmentRow:
    fragment = await get_interactive_fragment(fragment_id)
    if fragment is None:
        raise ToolFailure(f"No interactive fragment has the id {fragment_id}. list_fragments shows the ids.")
    return fragment


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


async def character_of(character_id: str, view: ViewContext) -> CharacterCardRow:
    """The character a tool names, else the one in view; a group view infers none and lists its cast instead."""
    card_id = character_id or view.character_id
    if not card_id and view.group and view.conversation_id:
        conv = await get_conversation(view.conversation_id)
        cast = await cast_of(conv) if conv else []
        raise ToolFailure(f"This is a group chat. Name one character_id from its cast: {json.dumps(cast)}")
    if not card_id:
        raise ToolFailure("No character is in view. Name a character_id; list_characters can find one.")
    card = await get_character_card(card_id)
    if card is None:
        raise ToolFailure(f"No character has the id {card_id}. list_characters shows the ids.")
    return card


async def reply_target(
    conversation_id: str, message_id: int, view: ViewContext, what: str, *, idle: bool
) -> tuple[ConversationRow, MessageRow]:
    """The chat, and the saved reply on its kept branch, that a handoff or a media tool names.

    *what* completes "only a reply can be …". *idle* also refuses a chat that is generating, as a handoff must: the chat's own
    stream would refuse to start.
    """
    conv = await chat_in_view(conversation_id, view, changes=True, idle=idle)
    cid = conv["id"]
    message = await message_of(cid, message_id)
    if message["role"] != "assistant":
        raise ToolFailure(f"Message {message['id']} is the user's own message; only a reply can be {what}.")
    if not any(row["id"] == message["id"] for row in await get_active_path(cid)):
        raise ToolFailure(
            f"Message {message['id']} is on a branch the chat is not showing, so it cannot be {what} there. "
            "switch_branch shows that branch first, if that is what the user wants."
        )
    return conv, message
