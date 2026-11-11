"""The assistant's actions: new lore entries, characters, worlds, personas and chats, a chat's worlds, title and persona,
checkpoints, branch selection, and handoffs to the chat's own stream or to the browser.

Creates are additive and go through each resource's own schema and writer, with server-generated ids. Chat mutations take the
chat's lane through :func:`~backend.api.deps.idle_chat_admission`, read their target again once admitted, and hold the lane only
for their own database work. A handoff writes nothing: it checks its target and names it, and the browser starts the work in
the chat's stream once this run has settled.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Annotated, Any

from pydantic import Field, StringConstraints, field_validator

from ...database import (
    create_character_card,
    create_lorebook_entry,
    create_user_persona,
    create_world,
    get_active_path,
    get_character_card,
    get_conversation,
    get_effective_world_ids,
    get_message_by_id,
    get_user_persona,
    get_world,
    get_worlds,
    set_conversation_world,
    switch_to_branch,
    update_conversation,
)
from ...database.models import ConversationRow, MessageRow
from ...features.assistant import AssistantTool, ToolArguments, ToolFailure, ToolOutput, ToolSet, ViewContext
from ..conversation_copy import checkpoint_conversation, checkpoint_title
from ..conversation_start import DEFAULT_TITLE, start_solo_chat
from ..deps import ChatUnavailable, conversation_busy, idle_chat_admission
from ..schemas import CharacterCardCreate, ConversationUpdate, LorebookEntryCreate, UserPersonaCreate, WorldCreate
from .targets import (
    ID_CHARS,
    cast_of,
    chat_field,
    chat_id,
    chat_in_view,
    clip,
    message_label,
    not_blank,
    quoted,
    reply_target,
)

_NAME_CHARS = 200
_LORE_CONTENT_CHARS = 4000
_LORE_KEYWORD_CHARS = 100
_LORE_KEYWORDS = 20
_LONG_TEXT_CHARS = 4000
_SHORT_TEXT_CHARS = 2000
_TAG_CHARS = 60
_TAGS = 20
_DIRECTION_CHARS = 500
_SEND_CHARS = 4000
_LISTED_WORLDS = 30


def _text(cap: int, description: str) -> Any:
    return Field(default="", max_length=cap, description=f"{description} At most {cap} characters.")


_Tag = Annotated[str, StringConstraints(min_length=1, max_length=_TAG_CHARS)]
_Keyword = Annotated[str, StringConstraints(min_length=1, max_length=_LORE_KEYWORD_CHARS)]


class _LoreEntryArgs(ToolArguments):
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The world (lorebook) to add the entry to.")
    name: str = Field(min_length=1, max_length=_NAME_CHARS, description="The entry's title.")
    content: str = Field(
        min_length=1, max_length=_LORE_CONTENT_CHARS, description=f"The lore itself, at most {_LORE_CONTENT_CHARS} characters."
    )
    keywords: list[_Keyword] = Field(
        default_factory=list,
        max_length=_LORE_KEYWORDS,
        description="Words that bring the entry into a chat when they come up.",
    )
    constant: bool = Field(default=False, description="Include the entry in every chat that uses the world, keywords or not.")

    _blank = field_validator("name", "content", "keywords")(not_blank)


class _CharacterCreate(ToolArguments):
    """Authored text only: no image, avatar, extension, embedded book, import source or URL can be named."""

    name: str = Field(
        min_length=1,
        max_length=_NAME_CHARS,
    )
    description: str = _text(_LONG_TEXT_CHARS, "Who the character is.")
    personality: str = _text(_SHORT_TEXT_CHARS, "Their personality.")
    scenario: str = _text(_SHORT_TEXT_CHARS, "The situation a chat with them starts in.")
    first_mes: str = _text(_LONG_TEXT_CHARS, "Their first message in a new chat.")
    mes_example: str = _text(_LONG_TEXT_CHARS, "Example messages showing how they talk.")
    creator_notes: str = _text(_SHORT_TEXT_CHARS, "Notes about the card for the user.")
    tags: list[_Tag] = Field(default_factory=list, max_length=_TAGS)

    _blank = field_validator("name", "tags")(not_blank)


class _WorldCreate(ToolArguments):
    name: str = Field(min_length=1, max_length=_NAME_CHARS, description="The world's (lorebook's) name.")

    _blank = field_validator("name")(not_blank)


class _PersonaCreate(ToolArguments):
    name: str = Field(min_length=1, max_length=_NAME_CHARS, description="The persona's name: who the user plays.")
    description: str = _text(_LONG_TEXT_CHARS, "Who that is, as the chats should know them.")

    _blank = field_validator("name")(not_blank)


class _ChatWorld(ToolArguments):
    conversation_id: str = chat_field()
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The world, from list_worlds.")
    attached: bool = Field(description="true to use the world in this chat, false to stop using it here.")


class _Checkpoint(ToolArguments):
    conversation_id: str = chat_field()
    title: str = Field(default="", max_length=_NAME_CHARS, description="The checkpoint's title; empty names it after the chat.")


class _Branch(ToolArguments):
    conversation_id: str = chat_field()
    message_id: int = Field(ge=1, description="A message on the branch to show, from query_library or read_messages.")


class _StartChat(ToolArguments):
    character_id: str = Field(min_length=1, max_length=ID_CHARS, description="The character, from list_characters.")
    title: str = Field(default="", max_length=_NAME_CHARS, description="The chat's title; empty names it after the character.")


class _OpenChat(ToolArguments):
    conversation_id: str = Field(min_length=1, max_length=ID_CHARS)


class _SetChat(ToolArguments):
    conversation_id: str = chat_field()
    title: str = Field(default="", max_length=_NAME_CHARS, description="A new title; empty leaves the title as it is.")
    persona_lock_id: int = Field(
        default=0, ge=0, description="Play as this persona in this chat, from list_personas; 0 leaves the lock as it is."
    )
    clear_persona_lock: bool = Field(
        default=False, description="true to stop locking a persona to this chat, so the active persona is used again."
    )


class _Continue(ToolArguments):
    conversation_id: str = chat_field()
    speaker_member_id: str = Field(
        default="",
        max_length=ID_CHARS,
        description="In a group chat: who speaks next, as a member_id from get_context's cast or a member's name. "
        "Empty in a solo chat.",
    )


class _SendAsUser(ToolArguments):
    content: str = Field(
        min_length=1,
        max_length=_SEND_CHARS,
        description="The user's message as they would type it, in their persona's voice.",
    )
    conversation_id: str = chat_field()

    _blank = field_validator("content")(not_blank)


class _Regenerate(ToolArguments):
    conversation_id: str = chat_field()
    message_id: int = Field(ge=1, description="The reply, from read_messages.")


class _Rewrite(_Regenerate):
    direction: str = Field(min_length=1, max_length=_DIRECTION_CHARS, description="How to rewrite it, in the user's words.")

    _blank = field_validator("direction")(not_blank)


def build_action_tools(view: ViewContext) -> list[AssistantTool]:
    """The creates, chat actions and handoffs, bound to the view the request was sent from."""

    tools = ToolSet()

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

    @tools.add(
        "write",
        _LoreEntryArgs,
        "Add a new entry to a world (lorebook).",
        name="create_lorebook_entry",
    )
    async def add_lorebook_entry(args: _LoreEntryArgs) -> ToolOutput:
        world = await get_world(args.world_id)
        if world is None:
            raise ToolFailure(f"No world has the id {args.world_id}. list_worlds shows the ids.")
        # The entry route's own schema and writer: an authored entry with its defaults, and a world revision bump.
        fields = LorebookEntryCreate(name=args.name, content=args.content, keywords=list(args.keywords), constant=args.constant)
        entry = await create_lorebook_entry(world["id"], fields.model_dump())
        world_name = clip(world["name"], _NAME_CHARS)
        data = {
            "created": True,
            "world": {"id": world["id"], "name": world_name},
            "entry": {
                "id": entry["id"],
                "name": clip(entry["name"], _NAME_CHARS),
                "keywords": entry["keywords"] or [],
                "constant": bool(entry["constant"]),
                "enabled": bool(entry["enabled"]),
            },
        }
        return ToolOutput(
            data,
            f"Created lore entry {json.dumps(data['entry']['name'], ensure_ascii=False)} in {world_name}.",
            {"world_id": world["id"], "entry_id": entry["id"]},
            committed=({"kind": "lorebook_entry", "world_id": world["id"], "entry_id": entry["id"]},),
        )

    @tools.add(
        "write",
        _CharacterCreate,
        "Create a character card from authored text.",
        name="create_character",
    )
    async def add_character(args: _CharacterCreate) -> ToolOutput:
        # The creation route's own schema and writer, without the parts of that route that import: no embedded book, no
        # extensions, and no expression pack fetched from a URL.
        card = CharacterCardCreate(**args.model_dump()).model_dump()
        card.pop("character_book", None)
        card["id"] = str(uuid.uuid4())
        card["source_format"] = "manual"
        created = await create_character_card(card)
        card_id = str(card["id"])
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

    @tools.add(
        "write",
        _WorldCreate,
        "Create an empty world (lorebook); it is not global and no chat uses it yet.",
        name="create_world",
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

    @tools.add(
        "write",
        _PersonaCreate,
        "Create a persona, someone the user plays; it does not become the active one.",
        name="create_persona",
    )
    async def add_persona(args: _PersonaCreate) -> ToolOutput:
        persona = await create_user_persona(UserPersonaCreate(name=args.name, description=args.description).model_dump())
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

    @tools.add(
        "write",
        _ChatWorld,
        "Use a world in one chat, or stop using it there. "
        "It changes nothing else about the chat and reports whether the world is global.",
        idempotent=True,
    )
    async def set_chat_world(args: _ChatWorld) -> ToolOutput:
        cid = chat_id(args.conversation_id, view)
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

    @tools.add(
        "write",
        _Checkpoint,
        "Copy a chat's kept branch into a new, separate chat. The chat in view is not switched.",
    )
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

    @tools.add(
        "write",
        _Branch,
        "Show the branch containing one message in a chat, as the branch arrows do. The request ends once it has "
        "switched, since the chat then reads differently.",
        name="switch_branch",
        idempotent=True,
    )
    async def select_branch(args: _Branch) -> ToolOutput:
        cid = chat_id(args.conversation_id, view)
        async with admitted(cid) as conv:
            message = await owned_message(cid, args.message_id)
            ids = {"conversation_id": cid, "message_id": message["id"]}
            path = await get_active_path(cid)
            if any(row["id"] == message["id"] for row in path):
                data = {"switched": False, **ids, "note": "That message is already on the branch the chat shows."}
                label = message_label(path, message["id"])
                return ToolOutput(data, f"{label.capitalize()} is already on the kept branch; nothing changed.", ids)
            # The branch route's own selection: the newest line of replies below that message.
            if not await switch_to_branch(cid, message["id"]):
                raise ToolFailure(f"That chat has no message {args.message_id}.")
            current = await get_conversation(cid)
            label = message_label(await get_active_path(cid), message["id"])
        leaf = current.get("active_leaf_id") if current else None
        data = {"switched": True, **ids, "active_leaf_id": leaf}
        return ToolOutput(
            data,
            f"Switched {quoted(conv['title'])} to the branch with {label}.",
            ids,
            committed=({"kind": "branch", "conversation_id": cid},),
            ends_run=True,
        )

    async def handed(conv: ConversationRow, message: Mapping[str, Any], doing: str, extra: Mapping[str, Any]) -> ToolOutput:
        target = {"conversation_id": conv["id"], "message_id": message["id"], **extra}
        data = {"handed_off": True, **target}
        label = message_label(await get_active_path(conv["id"]), message["id"])
        return ToolOutput(
            data,
            f"Handed {doing} {label} in {quoted(conv['title'])} to the chat.",
            {"conversation_id": conv["id"], "message_id": message["id"]},
            handoff=target,
        )

    @tools.add(
        "handoff",
        _Regenerate,
        "Have the chat regenerate one reply on its kept branch, as its Regenerate button does: a new version beside "
        "the old one.",
    )
    async def regenerate_reply(args: _Regenerate) -> ToolOutput:
        conv, message = await reply_target(args.conversation_id, args.message_id, view, "regenerated", idle=True)
        if message["parent_id"] is None:
            raise ToolFailure(f"Message {message['id']} is the chat's opening greeting, which is not regenerated.")
        return await handed(conv, message, "regenerating", {})

    @tools.add(
        "handoff",
        _Rewrite,
        "Have the chat rewrite one reply on its kept branch in a direction, as Magic Rewrite does: a new version "
        "beside the old one.",
    )
    async def rewrite_reply(args: _Rewrite) -> ToolOutput:
        conv, message = await reply_target(args.conversation_id, args.message_id, view, "rewritten", idle=True)
        return await handed(conv, message, "rewriting", {"direction": args.direction})

    @tools.add(
        "write",
        _StartChat,
        "Start a new chat with a character, with its greeting as the first reply, as the New Chat button does. It opens "
        "nothing; open_chat opens it if the user asks.",
    )
    async def start_chat(args: _StartChat) -> ToolOutput:
        card = await get_character_card(args.character_id)
        if card is None:
            raise ToolFailure(f"No character has the id {args.character_id}. list_characters shows the ids.")
        conv = await start_solo_chat(args.title.strip() or DEFAULT_TITLE, card)
        data = {
            "created": True,
            "conversation": {"id": conv["id"], "title": clip(conv["title"], _NAME_CHARS)},
            "character": {"id": args.character_id, "name": clip(card.get("name"), _NAME_CHARS)},
            "note": "It is in the chat list. Nothing was opened; the view is unchanged.",
        }
        return ToolOutput(
            data,
            f"Started chat {quoted(conv['title'])} with {quoted(card.get('name'))}.",
            {"conversation_id": conv["id"], "character_id": args.character_id},
            committed=({"kind": "conversation", "conversation_id": conv["id"]},),
        )

    @tools.add(
        "handoff",
        _OpenChat,
        "Open a chat in the main view once this request ends, as picking it in the chat list does.",
    )
    async def open_chat(args: _OpenChat) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=True)
        target = {"conversation_id": conv["id"], "title": clip(conv["title"], _NAME_CHARS)}
        data = {"handed_off": True, **target}
        return ToolOutput(
            data,
            f"Handed opening chat {quoted(conv['title'])} to the browser.",
            {"conversation_id": conv["id"]},
            handoff=target,
        )

    @tools.add(
        "write",
        _SetChat,
        "Rename a chat, or choose the persona the user plays in it.",
        idempotent=True,
    )
    async def set_chat(args: _SetChat) -> ToolOutput:
        if args.persona_lock_id and args.clear_persona_lock:
            raise ToolFailure("Either lock a persona or clear the lock, not both. Nothing was changed.")
        if not (args.title.strip() or args.persona_lock_id or args.clear_persona_lock):
            raise ToolFailure("Name a title, a persona_lock_id, or clear_persona_lock; nothing was asked to change.")
        persona = await get_user_persona(args.persona_lock_id) if args.persona_lock_id else None
        if args.persona_lock_id and persona is None:
            raise ToolFailure(f"No persona has the id {args.persona_lock_id}. list_personas shows the ids.")
        cid = chat_id(args.conversation_id, view)
        async with admitted(cid) as conv:
            wanted = ConversationUpdate(
                **({"title": args.title.strip()} if args.title.strip() else {}),
                **({"persona_lock_id": args.persona_lock_id} if args.persona_lock_id else {}),
                **({"persona_lock_id": None} if args.clear_persona_lock else {}),
            ).model_dump(exclude_unset=True)
            # Only what differs is written: a title or lock already in that state stays as stored.
            changes = {key: value for key, value in wanted.items() if conv.get(key) != value}
            current = await update_conversation(cid, changes) if changes else conv
        if current is None:
            raise ToolFailure(f"No chat has the id {cid}.")
        lock = current.get("persona_lock_id")
        data = {
            "conversation_id": cid,
            "title": clip(current["title"], _NAME_CHARS),
            "persona_lock_id": lock,
            "changed": sorted(changes),
        }
        if lock is None:
            data["note"] = "No persona is locked to this chat: it uses the character's lock, if any, or the active persona."
        if not changes:
            return ToolOutput(data, f"{quoted(current['title'])} already had that; nothing changed.", {"conversation_id": cid})
        said = []
        if "title" in changes:
            said.append(f"renamed it {quoted(current['title'])}")
        if "persona_lock_id" in changes:
            said.append(f"locked persona {quoted(persona['name'])} to it" if persona else "cleared its persona lock")
        return ToolOutput(
            data,
            f"Chat {quoted(conv['title'])}: {' and '.join(said)}.",
            {"conversation_id": cid},
            committed=({"kind": "conversation", "conversation_id": cid},),
        )

    @tools.add(
        "handoff",
        _Continue,
        "Have the chat write the next reply, as Send with an empty box does: an answer to the user's unanswered last "
        "message, or in a group chat a turn by the member named. A solo chat's character never replies twice in a row.",
    )
    async def continue_chat(args: _Continue) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=True)
        cid = conv["id"]
        if conversation_busy(cid):
            raise ToolFailure("That chat is generating right now. Ask again once it has finished.")
        path = await get_active_path(cid)
        last = path[-1] if path else None
        group = conv.get("kind") == "group"
        cast = await cast_of(conv)
        speaker = args.speaker_member_id.strip()
        if speaker and not group:
            raise ToolFailure("speaker_member_id is only for a group chat; leave it empty here.")
        if speaker:
            # A member id, or a name only one member has: what the user said is usually the name.
            named = [item for item in cast if str(item["name"]).casefold() == speaker.casefold()]
            member = next((item for item in cast if item.get("member_id") == speaker), None) or (
                named[0] if len(named) == 1 else None
            )
            if member is None:
                raise ToolFailure(f"That group has no member {speaker}. Its cast: {json.dumps(cast, ensure_ascii=False)}")
            speaker = str(member["member_id"])
            if member.get("muted"):
                raise ToolFailure(f"{quoted(member['name'])} is muted in this scene and does not speak.")
        if last is None:
            raise ToolFailure("That chat has no messages yet; the user writes the first one.")
        if last["role"] == "assistant" and not speaker:
            if group:
                raise ToolFailure(
                    "The last message is already a reply. Name the member who speaks next as speaker_member_id: "
                    f"{json.dumps(cast, ensure_ascii=False)}"
                )
            raise ToolFailure(
                "The last message is already a reply, and a solo chat's character never replies twice in a row: the "
                "user writes next. send_as_user writes and sends the user's message, if they asked for that; "
                "regenerate_reply makes a new version of the reply."
            )
        target = {
            "conversation_id": cid,
            "message_id": last["id"],
            **({"speaker_member_id": speaker} if speaker else {}),
        }
        data = {"handed_off": True, **target}
        who = f" by {quoted(next(m['name'] for m in cast if m.get('member_id') == speaker))}" if speaker else ""
        return ToolOutput(
            data,
            f"Handed the next reply{who} in {quoted(conv['title'])} to the chat.",
            {"conversation_id": cid, "message_id": last["id"]},
            handoff=target,
        )

    @tools.add(
        "handoff",
        _SendAsUser,
        "Write the user's next message and send it, as typing in the box and pressing Send does: it is posted as the "
        "user's, and the chat answers it. Only when the user asks for their side to be written.",
    )
    async def send_as_user(args: _SendAsUser) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=True)
        cid = conv["id"]
        if conversation_busy(cid):
            raise ToolFailure("That chat is generating right now. Ask again once it has finished.")
        path = await get_active_path(cid)
        if not path:
            raise ToolFailure("That chat has no messages yet; the user sends the first one from the box.")
        last = path[-1]
        if last["role"] == "user":
            raise ToolFailure(
                f"The user's last message, {message_label(path, last['id'])}, is still unanswered, so Send takes no "
                "second one. continue_chat has it answered."
            )
        target = {"conversation_id": cid, "message_id": last["id"]}
        return ToolOutput(
            {"handed_off": True, **target},
            f"Handed the user's message in {quoted(conv['title'])} to the chat to send.",
            target,
            handoff={**target, "content": args.content},
        )

    return tools
