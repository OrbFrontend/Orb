"""The assistant's remaining actions: new characters, worlds and personas, a chat's worlds, checkpoints, branch selection, and
handoffs of a reply to the chat's own stream.

Creates are additive and go through each resource's own schema and writer, with server-generated ids. Chat mutations take the
chat's lane through :func:`~backend.api.deps.idle_chat_admission`, read their target again once admitted, and hold the lane only
for their own database work. A handoff writes nothing: it checks its target and names it, and the browser starts the work in
the chat's stream once this run has settled.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any, TypeVar

from pydantic import Field, StringConstraints, field_validator

from ..database import (
    create_character_card,
    create_user_persona,
    create_world,
    get_active_path,
    get_conversation,
    get_effective_world_ids,
    get_message_by_id,
    get_world,
    get_worlds,
    set_conversation_world,
    switch_to_branch,
)
from ..database.models import ConversationRow, MessageRow
from ..features.assistant import AssistantTool, ToolArguments, ToolFailure, ToolOutput, ViewContext
from .assistant_edits import ID_CHARS, ReadRecord, chat_id, clip, not_blank, quoted, reply_target
from .conversation_copy import checkpoint_conversation, checkpoint_title
from .deps import ChatUnavailable, idle_chat_admission
from .schemas import CharacterCardCreate, UserPersonaCreate, WorldCreate

_Args = TypeVar("_Args", bound=ToolArguments)

_NAME_CHARS = 200
_LONG_TEXT_CHARS = 4000
_SHORT_TEXT_CHARS = 2000
_TAG_CHARS = 60
_TAGS = 20
_DIRECTION_CHARS = 500
_LISTED_WORLDS = 30


def _text(cap: int, description: str) -> Any:
    return Field(default="", max_length=cap, description=f"{description} At most {cap} characters.")


def _chat_field() -> Any:
    return Field(default="", max_length=ID_CHARS, description="The chat's id; empty means the chat in view.")


_Tag = Annotated[str, StringConstraints(min_length=1, max_length=_TAG_CHARS)]


class _CharacterCreate(ToolArguments):
    """Authored text only: no image, avatar, extension, embedded book, import source or URL can be named."""

    name: str = Field(min_length=1, max_length=_NAME_CHARS, description="The character's name.")
    description: str = _text(_LONG_TEXT_CHARS, "Who the character is.")
    personality: str = _text(_SHORT_TEXT_CHARS, "Their personality.")
    scenario: str = _text(_SHORT_TEXT_CHARS, "The situation a chat with them starts in.")
    first_mes: str = _text(_LONG_TEXT_CHARS, "Their first message in a new chat.")
    mes_example: str = _text(_LONG_TEXT_CHARS, "Example messages showing how they talk.")
    creator_notes: str = _text(_SHORT_TEXT_CHARS, "Notes about the card for the user.")
    tags: list[_Tag] = Field(default_factory=list, max_length=_TAGS, description="Library tags.")

    _blank = field_validator("name", "tags")(not_blank)


class _WorldCreate(ToolArguments):
    name: str = Field(min_length=1, max_length=_NAME_CHARS, description="The world's (lorebook's) name.")

    _blank = field_validator("name")(not_blank)


class _PersonaCreate(ToolArguments):
    name: str = Field(min_length=1, max_length=_NAME_CHARS, description="The persona's name: who the user plays.")
    description: str = _text(_LONG_TEXT_CHARS, "Who that is, as the chats should know them.")

    _blank = field_validator("name")(not_blank)


class _ChatWorld(ToolArguments):
    conversation_id: str = _chat_field()
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The world, from list_worlds.")
    attached: bool = Field(description="true to use the world in this chat, false to stop using it here.")


class _Checkpoint(ToolArguments):
    conversation_id: str = _chat_field()
    title: str = Field(default="", max_length=_NAME_CHARS, description="The checkpoint's title; empty names it after the chat.")


class _Branch(ToolArguments):
    conversation_id: str = _chat_field()
    message_id: int = Field(ge=1, description="A message on the branch to show, from query_library or read_messages.")


class _Regenerate(ToolArguments):
    conversation_id: str = _chat_field()
    message_id: int = Field(ge=1, description="The reply to regenerate, from read_messages.")


class _Rewrite(ToolArguments):
    conversation_id: str = _chat_field()
    message_id: int = Field(ge=1, description="The reply to rewrite, from read_messages.")
    direction: str = Field(min_length=1, max_length=_DIRECTION_CHARS, description="How to rewrite it, in the user's words.")

    _blank = field_validator("direction")(not_blank)


def build_action_tools(view: ViewContext, record: ReadRecord) -> list[AssistantTool]:
    """This milestone's creates, chat actions and handoffs, bound to the view the request was sent from.

    A create notes in *record* each text it stored exactly as sent: this run wrote it, so it has seen it whole.
    """

    @asynccontextmanager
    async def admitted(cid: str) -> AsyncIterator[ConversationRow]:
        """Hold chat *cid*'s lane through a short mutation, its row read again once admitted."""
        try:
            async with idle_chat_admission(cid):
                conv = await get_conversation(cid)
                if conv is None:
                    raise ToolFailure(f"No chat has the id {cid}.")
                yield conv
        except ChatUnavailable as exc:
            if exc.reason == "deleting":
                raise ToolFailure("That chat is being deleted, so nothing was changed.") from None
            raise ToolFailure(
                "That chat is generating right now, so nothing was changed. Ask again once it has finished."
            ) from None

    async def add_character(args: _CharacterCreate) -> ToolOutput:
        # The creation route's own schema and writer, without the parts of that route that import: no embedded book, no
        # extensions, and no expression pack fetched from a URL.
        card = CharacterCardCreate(
            name=args.name,
            description=args.description,
            personality=args.personality,
            scenario=args.scenario,
            first_mes=args.first_mes,
            mes_example=args.mes_example,
            creator_notes=args.creator_notes,
            tags=list(args.tags),
        ).model_dump()
        card.pop("character_book", None)
        card["id"] = str(uuid.uuid4())
        card["source_format"] = "manual"
        created = await create_character_card(card)
        card_id = str(card["id"])
        for name in ("description", "personality", "scenario", "first_mes", "mes_example", "creator_notes"):
            record.shown("character", card_id, name, str(created.get(name) or ""), getattr(args, name))
        data = {
            "created": True,
            "character": {"id": card_id, "name": clip(created.get("name"), _NAME_CHARS)},
            "note": "It is in the character library. No chat was started and nothing was opened.",
        }
        return ToolOutput(
            data,
            f"Created character {quoted(created.get('name'))}.",
            {"character_id": card_id},
            committed=({"kind": "character", "character_id": card_id},),
        )

    async def add_world(args: _WorldCreate) -> ToolOutput:
        # Never global and never attached: either is a separate choice the user makes.
        world = await create_world(WorldCreate(name=args.name, is_global=False).model_dump())
        data = {
            "created": True,
            "world": {"id": world["id"], "name": clip(world["name"], _NAME_CHARS), "is_global": bool(world["is_global"])},
            "note": "It is empty, not global, and used by no chat; set_chat_world adds it to a chat.",
        }
        return ToolOutput(
            data,
            f"Created world {quoted(world['name'])}.",
            {"world_id": world["id"]},
            committed=({"kind": "world", "world_id": world["id"]},),
        )

    async def add_persona(args: _PersonaCreate) -> ToolOutput:
        persona = await create_user_persona(UserPersonaCreate(name=args.name, description=args.description).model_dump())
        record.shown("persona", persona["id"], "name", persona["name"] or "", args.name)
        record.shown("persona", persona["id"], "description", persona["description"] or "", args.description)
        data = {
            "created": True,
            "persona": {"id": persona["id"], "name": clip(persona["name"], _NAME_CHARS)},
            "note": "It is not the active persona; the user chooses that.",
        }
        return ToolOutput(
            data,
            f"Created persona {quoted(persona['name'])}.",
            {"persona_id": persona["id"]},
            committed=({"kind": "persona", "persona_id": persona["id"]},),
        )

    async def set_chat_world(args: _ChatWorld) -> ToolOutput:
        cid = chat_id(args.conversation_id, view)
        if await get_world(args.world_id) is None:
            raise ToolFailure(f"No world has the id {args.world_id}. list_worlds shows the ids.")
        async with admitted(cid) as conv:
            world = await get_world(args.world_id)
            if world is None:
                raise ToolFailure(f"No world has the id {args.world_id}. list_worlds shows the ids.")
            before = world["id"] in await get_effective_world_ids(cid)
            # Only a change is written: the chat's other world choices, and an explicit choice already in this state, stay.
            if before != args.attached:
                await set_conversation_world(cid, world["id"], args.attached)
            effective = await get_effective_world_ids(cid)
        attached = world["id"] in effective
        names = {item["id"]: item["name"] for item in await get_worlds()}
        is_global = bool(world["is_global"])
        data: dict[str, Any] = {
            "conversation_id": cid,
            "world": {"id": world["id"], "name": clip(world["name"], _NAME_CHARS), "is_global": is_global},
            "requested": args.attached,
            "attached": attached,
            "changed": attached != before,
            "effective_worlds": [
                {"id": world_id, "name": clip(names.get(world_id), _NAME_CHARS)} for world_id in effective[:_LISTED_WORLDS]
            ],
        }
        if is_global:
            data["note"] = (
                "This world is global: every chat uses it unless that chat turns it off. This changed only this chat."
            )
        state = "uses" if attached else "does not use"
        summary = (
            f"{'Now' if attached != before else 'Already'}: {quoted(conv['title'])} {state} world {quoted(world['name'])}."
        )
        committed = ({"kind": "chat_worlds", "conversation_id": cid, "world_id": world["id"]},) if attached != before else ()
        return ToolOutput(data, summary, {"conversation_id": cid, "world_id": world["id"]}, committed=committed)

    async def checkpoint_chat(args: _Checkpoint) -> ToolOutput:
        cid = chat_id(args.conversation_id, view)
        async with admitted(cid) as conv:
            # The checkpoint route's own copy, under the chat's lane so no reply lands half-way through it.
            copy = await checkpoint_conversation(cid, checkpoint_title(conv, args.title))
        if copy is None:
            raise ToolFailure(f"No chat has the id {cid}.")
        data = {
            "created": True,
            "checkpoint": {"id": copy["id"], "title": clip(copy["title"], _NAME_CHARS)},
            "source_conversation_id": cid,
            "note": "A separate chat holding a copy of the kept branch. The chat in view is unchanged and was not switched.",
        }
        return ToolOutput(
            data,
            f"Created checkpoint {quoted(copy['title'])} of {quoted(conv['title'])}.",
            {"conversation_id": copy["id"], "source_conversation_id": cid},
            committed=({"kind": "conversation", "conversation_id": copy["id"]},),
        )

    async def owned_message(cid: str, message_id: int) -> MessageRow:
        message = await get_message_by_id(message_id)
        if message is None or message["conversation_id"] != cid:
            raise ToolFailure(f"That chat has no message {message_id}. read_messages or query_library shows the ids.")
        return message

    async def select_branch(args: _Branch) -> ToolOutput:
        cid = chat_id(args.conversation_id, view)
        async with admitted(cid) as conv:
            message = await owned_message(cid, args.message_id)
            ids = {"conversation_id": cid, "message_id": message["id"]}
            if any(row["id"] == message["id"] for row in await get_active_path(cid)):
                data = {"switched": False, **ids, "note": "That message is already on the branch the chat shows."}
                return ToolOutput(data, f"Message {message['id']} is already on the kept branch; nothing changed.", ids)
            # The branch route's own selection: the newest line of replies below that message.
            if not await switch_to_branch(cid, message["id"]):
                raise ToolFailure(f"That chat has no message {args.message_id}.")
            current = await get_conversation(cid)
        leaf = current.get("active_leaf_id") if current else None
        data = {"switched": True, **ids, "active_leaf_id": leaf}
        return ToolOutput(
            data,
            f"Switched {quoted(conv['title'])} to the branch with message {message['id']}.",
            ids,
            committed=({"kind": "branch", "conversation_id": cid},),
            ends_run=True,
        )

    def handed(conv: ConversationRow, message: Mapping[str, Any], doing: str, extra: Mapping[str, Any]) -> ToolOutput:
        target = {"conversation_id": conv["id"], "message_id": message["id"], **extra}
        data = {
            "handed_off": True,
            **target,
            "note": "Nothing is generated yet. Once this request ends the chat starts it, if that chat is still open, as a "
            "new version beside the old one; the chat writes and saves the reply.",
        }
        return ToolOutput(
            data,
            f"Handed {doing} message {message['id']} in {quoted(conv['title'])} to the chat.",
            {"conversation_id": conv["id"], "message_id": message["id"]},
            handoff=target,
        )

    async def regenerate_reply(args: _Regenerate) -> ToolOutput:
        conv, message = await reply_target(args.conversation_id, args.message_id, view, "regenerated", idle=True)
        if message["parent_id"] is None:
            raise ToolFailure(f"Message {message['id']} is the chat's opening greeting, which is not regenerated.")
        return handed(conv, message, "regenerating", {})

    async def rewrite_reply(args: _Rewrite) -> ToolOutput:
        conv, message = await reply_target(args.conversation_id, args.message_id, view, "rewritten", idle=True)
        return handed(conv, message, "rewriting", {"direction": args.direction})

    def write(
        name: str,
        description: str,
        arguments: type[_Args],
        run: Callable[[_Args], Awaitable[ToolOutput]],
        target: Callable[[_Args], Mapping[str, Any]],
        *,
        idempotent: bool = False,
    ) -> AssistantTool:
        return AssistantTool(name, description, arguments, "write", run, target=target, idempotent=idempotent)

    def handoff(name: str, description: str, arguments: type[_Args], run: Callable[[_Args], Awaitable[ToolOutput]]):
        return AssistantTool(name, description, arguments, "handoff", run)

    additive = " It always adds a new one, even when one already has that name; it never edits one."
    return [
        write(
            "create_character",
            "Create a character card from authored text." + additive,
            _CharacterCreate,
            add_character,
            lambda args: {"kind": "character"},
        ),
        write(
            "create_world",
            "Create an empty world (lorebook); it is not global and no chat uses it yet." + additive,
            _WorldCreate,
            add_world,
            lambda args: {"kind": "world"},
        ),
        write(
            "create_persona",
            "Create a persona, someone the user plays; it does not become the active one." + additive,
            _PersonaCreate,
            add_persona,
            lambda args: {"kind": "persona"},
        ),
        write(
            "set_chat_world",
            "Use a world in one chat, or stop using it there. Give the state the user asked for, never a flip. "
            "It changes nothing else about the chat and reports whether the world is global.",
            _ChatWorld,
            set_chat_world,
            lambda args: {
                "kind": "chat_worlds",
                "conversation_id": args.conversation_id or view.conversation_id,
                "world_id": args.world_id,
            },
            idempotent=True,
        ),
        write(
            "checkpoint_chat",
            "Copy a chat's kept branch into a new, separate chat. The chat in view is not switched.",
            _Checkpoint,
            checkpoint_chat,
            lambda args: {"kind": "conversation"},
        ),
        write(
            "switch_branch",
            "Show the branch containing one message in a chat, as the branch arrows do. The request ends once it has "
            "switched, since the chat then reads differently.",
            _Branch,
            select_branch,
            lambda args: {"kind": "branch", "conversation_id": args.conversation_id or view.conversation_id},
            idempotent=True,
        ),
        handoff(
            "regenerate_reply",
            "Have the chat regenerate one reply on its kept branch, as its Regenerate button does: a new version beside "
            "the old one.",
            _Regenerate,
            regenerate_reply,
        ),
        handoff(
            "rewrite_reply",
            "Have the chat rewrite one reply on its kept branch in a direction, as Magic Rewrite does: a new version "
            "beside the old one.",
            _Rewrite,
            rewrite_reply,
        ),
    ]
