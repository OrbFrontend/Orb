"""The assistant's deletes: each proposes removing one thing and removes nothing.

A delete cannot be undone, so it is never a write the run makes: it becomes a proposal on the user's card, which says what goes
with it, and only their Apply deletes, through the same path and fences as the app's own delete.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from ....database import (
    get_active_path,
    get_character_usage,
    get_lorebook_entries,
    get_phrase_bank_rows,
)
from ....database.queries.messages import get_message_subtree_ids
from ..contracts import AssistantTool, ToolArguments, ToolFailure, ToolOutput, ToolSet, ViewContext
from .edits import proposal_key
from .host import ChatGuards
from .proposals import Proposal, ProposalKind, is_open, preview, store
from .targets import (
    character_field,
    character_of,
    chat_field,
    chat_in_view,
    entry_field,
    entry_of,
    message_label,
    message_of,
    quoted,
    world_field,
    world_of,
)


class _CharacterDelete(ToolArguments):
    character_id: str = character_field()
    delete_chats: bool = Field(
        default=False,
        description="true only when the user asks for its chats to go too.",
    )


class _ChatDelete(ToolArguments):
    conversation_id: str = chat_field()


class _MessageDelete(ToolArguments):
    conversation_id: str = chat_field()
    message_id: int = Field(ge=1, description="The message, from read_messages. Every message after it goes too.")


class _WorldDelete(ToolArguments):
    world_id: str = world_field()


class _EntryDelete(ToolArguments):
    world_id: str = world_field()
    entry_id: int = entry_field()


class _PhraseDelete(ToolArguments):
    group_id: int = Field(ge=1, description="The phrase group, from list_banned_phrases.")


def _count(n: int, noun: str) -> str:
    return f"{n:,} {noun}{'' if n == 1 else 's'}"


def build_delete_tools(view: ViewContext, guards: ChatGuards, held: Sequence[Proposal] = ()) -> list[AssistantTool]:
    """The delete tools, bound to the request's view and to *held*, the pending proposals earlier requests made."""
    tools = ToolSet()
    proposed: dict[str, Proposal] = {proposal_key(item.target, item.name): item for item in held if item.action == "delete"}

    def propose(
        kind: ProposalKind, target: dict[str, Any], label: str, detail: str, options: Mapping[str, Any] | None = None
    ) -> ToolOutput:
        """Hold a delete of *target*; *detail* is what the card tells the user goes with it."""
        key = proposal_key(target, "delete")
        earlier = proposed.get(key)
        if earlier is not None and is_open(earlier) and dict(earlier.options) == dict(options or {}):
            raise ToolFailure(f"Deleting {label} is already proposed and waits for the user's Apply. Do not propose it again.")
        proposal = store(kind, target, label, "delete", detail, "", action="delete", options=options, replaces=earlier)
        proposed[key] = proposal
        data = {"proposed": f"deleting {label}", "detail": detail}
        ids = {"proposal_id": proposal.id, **{name: value for name, value in target.items() if name != "kind"}}
        return ToolOutput(
            data,
            f"Proposed deleting {label}; nothing is deleted until the user applies it.",
            ids,
            proposal=preview(proposal),
        )

    @tools.add(
        "confirm",
        _CharacterDelete,
        "Propose deleting a character card. Its chats are kept unless delete_chats, which deletes its solo chats and every "
        "group chat it is in.",
    )
    async def delete_character(args: _CharacterDelete) -> ToolOutput:
        card = await character_of(args.character_id, view)
        card_id = str(card.get("id"))
        usage = await get_character_usage(card_id)
        groups = usage["active_groups"] + usage["historical_groups"]
        chats = f"{_count(usage['solo'], 'solo chat')} and {_count(groups, 'group chat')}"
        detail = (
            f"Its {chats} are deleted with it."
            if args.delete_chats
            else f"Its {chats} are kept; they show no character until the card is imported again."
        )
        target = {"kind": "character", "character_id": card_id}
        return propose(
            "character", target, f"character {quoted(card.get('name'))}", detail, {"delete_chats": args.delete_chats}
        )

    @tools.add("confirm", _ChatDelete, "Propose deleting a chat with all its messages and branches.")
    async def delete_chat(args: _ChatDelete) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, guards, changes=True)
        path = await get_active_path(conv["id"])
        detail = f"All its messages go with it, on every branch; its kept branch holds {_count(len(path), 'message')}."
        target = {"kind": "conversation", "conversation_id": conv["id"]}
        return propose("conversation", target, f"chat {quoted(conv['title'])}", detail)

    @tools.add(
        "confirm",
        _MessageDelete,
        "Propose deleting one chat message with its other versions (swipes) and every message after them, on every branch, as "
        "the message's own Delete does.",
    )
    async def delete_message(args: _MessageDelete) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, guards, changes=True)
        cid = conv["id"]
        message = await message_of(cid, args.message_id)
        removed = len(await get_message_subtree_ids(cid, message["id"]))
        name = message_label(await get_active_path(cid), message["id"])
        others = removed - 1
        detail = (
            f"{_count(others, 'other message')} {'goes' if others == 1 else 'go'} with it: its other versions here and "
            "everything after, on every branch."
            if others
            else "No other version of it and no message after it."
        )
        target = {"kind": "message", "conversation_id": cid, "message_id": message["id"]}
        return propose("message", target, f"{name} in {quoted(conv['title'])}", detail)

    @tools.add(
        "confirm", _WorldDelete, "Propose deleting a world (lorebook) with all its entries; chats that use it stop using it."
    )
    async def delete_world(args: _WorldDelete) -> ToolOutput:
        world = await world_of(args.world_id, view)
        entries = len(await get_lorebook_entries(world["id"]))
        detail = f"Its {_count(entries, 'entry')} go with it, and every chat and character using it stops using it."
        return propose("world", {"kind": "world", "world_id": world["id"]}, f"world {quoted(world['name'])}", detail)

    @tools.add(
        "confirm",
        _EntryDelete,
        "Propose deleting one lore entry. To stop using it but keep it, turn it off with set_lore_entry instead.",
    )
    async def delete_lore_entry(args: _EntryDelete) -> ToolOutput:
        world, entry = await entry_of(args.world_id, args.entry_id, view)
        history = " The world keeps a record of it in its History." if world["dynamic_enabled"] else ""
        target = {"kind": "lorebook_entry", "world_id": world["id"], "entry_id": entry["id"]}
        label = f"lore entry {quoted(entry['name'])} in {quoted(world['name'])}"
        return propose("lorebook_entry", target, label, f"Its name, keywords and content go.{history}")

    @tools.add("confirm", _PhraseDelete, "Propose removing one phrase group from the phrase bank.")
    async def delete_banned_phrase(args: _PhraseDelete) -> ToolOutput:
        row = next((row for row in await get_phrase_bank_rows() if row["id"] == args.group_id), None)
        if row is None:
            raise ToolFailure(f"No phrase group has the id {args.group_id}. list_banned_phrases shows the ids.")
        variants = [str(variant) for variant in row["variants"]][:6]
        name = variants[0] if variants else str(row["pattern"] or args.group_id)
        detail = f"Replies may use these again: {', '.join(quoted(variant) for variant in variants) or quoted(name)}."
        target = {"kind": "phrase_group", "group_id": row["id"]}
        return propose("phrase_group", target, f"phrase group {quoted(name)}", detail)

    return tools
