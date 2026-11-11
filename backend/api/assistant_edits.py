"""The assistant's reviewed edits: tools that propose without writing, the proposals they leave here, and Apply.

A proposal is server-owned: it holds the canonical target, the fields it may change, the values the preview showed before and
after, and the request and view it came from. Apply names a proposal and nothing else, so a client cannot substitute
arguments or after values, and the database compares and writes in one transaction. Proposals live in this process only;
Orb runs one server process, and a restart leaves every proposal expired.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn, TypeVar

from fastapi import HTTPException
from pydantic import Field

from ..core.macros import resolve_inline
from ..database import (
    StaleWrite,
    get_active_path,
    get_character_card,
    get_conversation,
    get_lorebook_entry,
    get_message_by_id,
    get_user_persona,
    get_world,
    greeting_rerolls,
    resolve_cast,
    sync_conversations_for_card,
    update_character_card,
    update_lorebook_entry,
    update_user_persona,
)
from ..database.models import ConversationRow, MessageRow
from ..features.assistant import (
    MAX_ITEM_CHARS,
    AssistantTool,
    ToolArguments,
    ToolFailure,
    ToolOutput,
    ViewContext,
    settled,
)
from .deps import ChatUnavailable, conversation_busy, idle_chat_admission, resource_deleting
from .edits import save_message_edit

_Args = TypeVar("_Args", bound=ToolArguments)

MAX_PROPOSALS = 32
PROPOSAL_TTL_SECS = 15 * 60
# How long an id argument to any assistant tool may be.
ID_CHARS = 64
_LABEL_CHARS = 120
# What one argument may carry. Longer text is refused, never cut: a whole-field rewrite that long belongs in find_replace.
_EDIT_CHARS = MAX_ITEM_CHARS
# The longest field one paged read returns whole. A whole-field rewrite of a longer one is refused: the model cannot have seen
# all of it in one view, so its rewrite could drop text it never read.
FULL_FIELD_CHARS = 4000
# How a read marks the place it shortened a text. Edits refuse text carrying it: it is the reader's note, not the field's.
_CLIP_MARK = re.compile(r"…\[\+\d+ chars\]")
# The shortest any read clips a text an edit can name (a name in a listing). A field no longer than this is whole in every view
# that shows it; a longer one may be replaced whole only once this run has read all of it.
_MIN_CLIP_CHARS = 200

_ProposalKind = Literal["character", "lorebook_entry", "persona", "message"]
_CharacterField = Literal["description", "personality", "scenario", "first_mes", "mes_example", "creator_notes"]
_FIELD_LABELS = {
    "description": "Description",
    "personality": "Personality",
    "scenario": "Scenario",
    "first_mes": "First message",
    "mes_example": "Example messages",
    "creator_notes": "Creator notes",
    "name": "Name",
    "content": "Content",
}
_MODES = (
    "replace_all: `text` becomes the whole field, only for a field you have read whole. "
    "find_replace: `text` replaces `find`, copied exactly from the field, which must occur in it exactly once."
)


class ReadRecord:
    """The texts one run has read whole: each field as ``(kind, id, field)`` with a hash of the text read.

    A read adds a field when it returned the text unclipped, or as a page: pages that join up from the start cover the text, and
    the field is whole once they reach its end. The hash binds the record to the text as read, so a field changed since is not
    whole any more. Each run, a continuation after Apply included, starts with an empty record.
    """

    def __init__(self) -> None:
        self._covered: dict[tuple[str, str, str, str], int] = {}

    @staticmethod
    def _key(kind: str, resource: object, name: str, text: str) -> tuple[str, str, str, str]:
        return kind, str(resource), name, hashlib.sha256(text.encode()).hexdigest()

    def page(self, kind: str, resource: object, name: str, text: str, start: int, end: int) -> None:
        """Note that a read returned the part of *text* from *start* to *end*."""
        key = self._key(kind, resource, name, text)
        covered = self._covered.get(key, 0)
        if start <= covered:
            self._covered[key] = max(covered, end)

    def shown(self, kind: str, resource: object, name: str, text: str, returned: str) -> None:
        """Note what a read returned for a field: the whole text unless *returned* is shorter."""
        if returned == text:
            self.page(kind, resource, name, text, 0, len(text))

    def whole(self, kind: str, resource: object, name: str, text: str) -> bool:
        return len(text) <= _MIN_CLIP_CHARS or self._covered.get(self._key(kind, resource, name, text), 0) >= len(text)


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
    """A chat's cast as a tool shows it: each member's name and character id, and whether it is muted."""
    cast = await resolve_cast(conv)
    return [
        {"name": member.name, "character_id": member.card_id, **({"muted": True} if member.muted else {})}
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


@dataclass(frozen=True)
class Origin:
    """The request a proposal answers, with the view it was sent from as the browser named it.

    *approvals* are the server's notes of edits the user already applied for this request, carried from one continuation to
    the next, so a later continuation still knows of every earlier one.
    """

    request: str
    context: Mapping[str, Any]
    approvals: tuple[str, ...] = ()


@dataclass(eq=False)
class _Proposal:
    id: str
    kind: _ProposalKind
    # The canonical target as a committed resource: ``{"kind": ..., <ids>}``.
    target: Mapping[str, Any]
    label: str
    before: Mapping[str, str]
    after: Mapping[str, str]
    origin: Origin
    # When the store forgets it: its creation plus the expiry, and once it commits, that commit plus the expiry.
    expires: float
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Set the moment the guarded write commits. What follows it (a card's chats) is a step a retry repeats until ``result``.
    committed: bool = False
    # A message write that raised without saying whether its transaction committed. Only then can text already matching the
    # approved text be this proposal's own commit; otherwise another writer saved it, and this Apply wrote nothing.
    uncertain: bool = False
    # Set when the commit found the approved text already stored by another writer and wrote nothing.
    matched: bool = False
    # Once Apply has finished: its result, returned again to a repeated Apply. Once it is refused for good: the refusal.
    result: dict[str, Any] | None = None
    refusal: tuple[int, dict[str, Any]] | None = None
    continued: bool = False


_proposals: OrderedDict[str, _Proposal] = OrderedDict()
_clock: Callable[[], float] = time.monotonic


def _clean() -> None:
    """Forget expired proposals, except one an Apply is still working on."""
    now = _clock()
    for proposal_id in [key for key, item in _proposals.items() if item.expires < now and not item.lock.locked()]:
        del _proposals[proposal_id]


def _evict() -> bool:
    """Forget the oldest proposal nothing committed, else the oldest committed one; never one an Apply is working on."""
    idle = [item for item in _proposals.values() if not item.lock.locked()]
    victim = next((item for item in idle if not item.committed), idle[0] if idle else None)
    if victim is None:
        return False
    del _proposals[victim.id]
    return True


def _expired() -> NoReturn:
    raise HTTPException(
        status_code=410,
        detail={"code": "expired", "message": "This proposal has expired or is unknown to the server. Ask again."},
    )


def get_proposal(proposal_id: str) -> _Proposal:
    _clean()
    proposal = _proposals.get(proposal_id)
    if proposal is None:
        _expired()
    return proposal


def _occurrences(text: str, needle: str) -> int:
    """How often *needle* occurs in *text*, overlapping matches counted: each is a place the edit could mean."""
    count, start = 0, text.find(needle)
    while start != -1 and count < 2:
        count += 1
        start = text.find(needle, start + 1)
    return count


def _edited(before: str, args: Any, reread: str, *, read_whole: bool) -> str:
    """The field after *args*: a whole replacement, or one literal replacement of text that occurs exactly once.

    *reread* tells the model how to read this field whole, for a refusal that needs it to. *read_whole* says whether this run
    has read the field's current text whole; a whole replacement of a longer field needs it.
    """
    if _CLIP_MARK.search(args.text) and not _CLIP_MARK.search(before):
        raise ToolFailure(
            "The text contains a marker like …[+N chars]: a read puts it where it shortened a field, and it is not part of "
            f"the field. {reread} Then send only the field's real text. Nothing was proposed."
        )
    if args.mode == "replace_all":
        if args.find:
            raise ToolFailure("find is only for find_replace; with replace_all leave it empty. Nothing was proposed.")
        if len(before) > FULL_FIELD_CHARS:
            raise ToolFailure(
                f"That field is {len(before)} characters, more than one read shows whole ({FULL_FIELD_CHARS}), so replacing "
                "all of it could drop text you have not seen. Use find_replace on the part to change, once for each part. "
                f"{reread} Nothing was proposed."
            )
        if not read_whole:
            raise ToolFailure(
                f"That field is {len(before)} characters, and this request has not read all of its current text: a shortened "
                "view leaves part of it out, so replacing all of it could drop text you have not seen. Read the whole field "
                f"first, then send its complete new text, or use find_replace on the part to change. {reread} "
                "Nothing was proposed."
            )
        return args.text
    if not args.find:
        raise ToolFailure("find_replace needs the exact text to find, and find was empty. Nothing was proposed.")
    count = _occurrences(before, args.find)
    if not count:
        raise ToolFailure(f"The find text does not occur in that field. {reread} Then copy the text exactly.")
    if count > 1:
        raise ToolFailure("The find text occurs more than once. Include more of the text around it so it matches once.")
    return before.replace(args.find, args.text, 1)


def _propose(
    kind: _ProposalKind, target: Mapping[str, Any], label: str, name: str, before: str, after: str, origin: Origin
) -> ToolOutput:
    """Store a proposal for an edit the user has yet to approve, and the exact preview that Apply will commit."""
    if after == before:
        raise ToolFailure("That leaves the text exactly as it is, so there is nothing to propose.")
    _clean()
    while len(_proposals) >= MAX_PROPOSALS and _evict():
        pass
    now = _clock()
    proposal = _Proposal(
        secrets.token_hex(16), kind, dict(target), label, {name: before}, {name: after}, origin, expires=now + PROPOSAL_TTL_SECS
    )
    _proposals[proposal.id] = proposal
    what = f"{_FIELD_LABELS[name].lower()} of {label}"
    preview = {
        "proposal_id": proposal.id,
        "kind": kind,
        "target": dict(target),
        "label": label,
        "fields": [{"field": name, "label": _FIELD_LABELS[name], "before": before, "after": after}],
        "expires_in_s": PROPOSAL_TTL_SECS,
    }
    data = {
        "proposed": True,
        "applied": False,
        "proposal_id": proposal.id,
        "target": dict(target),
        "field": name,
        "note": "Nothing has changed. The user sees the preview and applies or cancels it; this request ends here.",
    }
    return ToolOutput(
        data,
        f"Proposed an edit to the {what}; nothing changes until it is applied.",
        {"proposal_id": proposal.id, **{key: value for key, value in target.items() if key != "kind"}},
        proposal=preview,
    )


def _mode_field() -> Any:
    return Field(description=_MODES)


def _text_field() -> Any:
    return Field(
        max_length=_EDIT_CHARS,
        description="replace_all: the field's complete new text. find_replace: what replaces `find`; empty deletes it.",
    )


def _find_field() -> Any:
    return Field(default="", max_length=_EDIT_CHARS, description="find_replace only: exact text from the field.")


class _CharacterEdit(ToolArguments):
    character_id: str = Field(
        default="", max_length=ID_CHARS, description="The character's id; empty means the character in view."
    )
    field: _CharacterField = Field(description="The card field to edit.")
    mode: Literal["replace_all", "find_replace"] = _mode_field()
    text: str = _text_field()
    find: str = _find_field()


class _EntryEdit(ToolArguments):
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The entry's world, from list_worlds.")
    entry_id: int = Field(ge=1, description="The entry's id, from list_lorebook_entries.")
    field: Literal["name", "content"] = Field(description="The entry field to edit.")
    mode: Literal["replace_all", "find_replace"] = _mode_field()
    text: str = _text_field()
    find: str = _find_field()


class _PersonaEdit(ToolArguments):
    persona_id: int = Field(ge=1, description="The persona's id, from list_personas.")
    field: Literal["name", "description"] = Field(description="The persona field to edit.")
    mode: Literal["replace_all", "find_replace"] = _mode_field()
    text: str = _text_field()
    find: str = _find_field()


class _MessageEdit(ToolArguments):
    conversation_id: str = Field(default="", max_length=ID_CHARS, description="The chat's id; empty means the chat in view.")
    message_id: int = Field(ge=1, description="The message's id, from read_messages.")
    mode: Literal["replace_all", "find_replace"] = _mode_field()
    text: str = _text_field()
    find: str = _find_field()


def _require_text(value: str, what: str) -> str:
    if not value.strip():
        raise ToolFailure(f"{what} cannot be left empty. Nothing was proposed.")
    return value


_GREETING_REROLLS = (
    "Message {id} is this chat's opening greeting, and it still holds random macros such as {{{{roll}}}} that roll again each "
    "time the chat is shown, until the user sends a first message. Its text changes on every view, so no preview of it could "
    "ever apply. Nothing was proposed. Tell the user to edit the greeting with the Edit button on it in the chat, which keeps "
    "the text as written; or, once they have sent a message, it stays fixed and can be edited here."
)


def build_edit_tools(view: ViewContext, origin: Origin, record: ReadRecord) -> list[AssistantTool]:
    """The four edit tools, bound to the request's view, to the request each proposal will remember, and to what this run's
    reads have shown whole."""

    async def edit_character(args: _CharacterEdit) -> ToolOutput:
        card_id = await character_in_view(args.character_id, view)
        card = await get_character_card(card_id)
        if card is None:
            raise ToolFailure(f"No character has the id {card_id}.")
        before = str(card.get(args.field) or "")
        reread = f'get_character with fields ["{args.field}"] reads it whole, one page per text_offset.'
        target = {"kind": "character", "character_id": card_id}
        label = f"character {quoted(card.get('name'))}"
        after = _edited(before, args, reread, read_whole=record.whole("character", card_id, args.field, before))
        return _propose("character", target, label, args.field, before, after, origin)

    async def edit_lorebook_entry(args: _EntryEdit) -> ToolOutput:
        world = await get_world(args.world_id)
        if world is None:
            raise ToolFailure(f"No world has the id {args.world_id}. list_worlds shows the ids.")
        entry = await get_lorebook_entry(args.entry_id)
        if entry is None or entry["world_id"] != world["id"]:
            raise ToolFailure(f"That world has no entry {args.entry_id}. list_lorebook_entries shows its entries.")
        before = str(entry[args.field] or "")
        reread = (
            "list_lorebook_entries with entry_id reads the entry whole: its name, and its content one page per text_offset."
        )
        after = _edited(before, args, reread, read_whole=record.whole("lorebook_entry", entry["id"], args.field, before))
        if args.field == "name":
            _require_text(after, "An entry's name")
        target = {"kind": "lorebook_entry", "world_id": world["id"], "entry_id": entry["id"]}
        label = f"lore entry {quoted(entry['name'])} in {quoted(world['name'])}"
        return _propose("lorebook_entry", target, label, args.field, before, after, origin)

    async def edit_persona(args: _PersonaEdit) -> ToolOutput:
        persona = await get_user_persona(args.persona_id)
        if persona is None:
            raise ToolFailure(f"No persona has the id {args.persona_id}. list_personas shows the ids.")
        before = str(persona[args.field] or "")
        reread = (
            "list_personas with persona_id reads the persona whole: its name, and its description one page per text_offset."
        )
        after = _edited(before, args, reread, read_whole=record.whole("persona", persona["id"], args.field, before))
        if args.field == "name":
            _require_text(after, "A persona's name")
        target = {"kind": "persona", "persona_id": persona["id"]}
        return _propose("persona", target, f"persona {quoted(persona['name'])}", args.field, before, after, origin)

    async def edit_message(args: _MessageEdit) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=True)
        cid = conv["id"]
        if conversation_busy(cid):
            raise ToolFailure("That chat is generating right now. Ask again once it has finished.")
        message = await get_message_by_id(args.message_id)
        if message is None or message["conversation_id"] != cid:
            raise ToolFailure(f"That chat has no message {args.message_id}. read_messages shows the ids.")
        if await greeting_rerolls(cid, message["id"]):
            raise ToolFailure(_GREETING_REROLLS.format(id=message["id"]))
        before = message["content"] or ""
        reread = "read_messages with message_id reads that message whole, one page per text_offset."
        # Inline macros resolve once, here, as the edit route resolves them on save: Apply commits this exact text, so a random
        # macro cannot roll a second time and save something the preview did not show.
        edited = _edited(before, args, reread, read_whole=record.whole("message", message["id"], "content", before))
        after = _require_text(resolve_inline(edited), "A message")
        target = {"kind": "message", "conversation_id": cid, "message_id": message["id"]}
        label = f"message {message['id']} in {quoted(conv['title'])}"
        return _propose("message", target, label, "content", before, after, origin)

    def tool(name: str, description: str, arguments: type[_Args], run: Callable[[_Args], Awaitable[ToolOutput]]):
        return AssistantTool(name, description, arguments, "confirm", run)

    suffix = " It changes nothing itself: the user sees the exact before and after, then applies or cancels it."
    return [
        tool(
            "edit_character",
            "Propose an edit to one of a character's text fields: description, personality, scenario, first_mes (first "
            "message), mes_example (example messages) or creator_notes." + suffix,
            _CharacterEdit,
            edit_character,
        ),
        tool(
            "edit_lorebook_entry",
            "Propose an edit to a lore entry's name or content." + suffix,
            _EntryEdit,
            edit_lorebook_entry,
        ),
        tool("edit_persona", "Propose an edit to a persona's name or description." + suffix, _PersonaEdit, edit_persona),
        tool(
            "edit_message",
            "Propose an edit to one chat message's text. Inline macros such as {{roll}} resolve in the preview." + suffix,
            _MessageEdit,
            edit_message,
        ),
    ]


class _Missing(Exception):
    """The proposal's target no longer exists."""


def _refuse(status: int, code: str, message: str, **extra: Any) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def _holds(stored: Mapping[str, Any], values: Mapping[str, str]) -> bool:
    """Whether every field in *values* is stored as it, a NULL column reading as empty text as the guarded write reads it."""
    return all((stored.get(name) or "") == (value or "") for name, value in values.items())


async def _already_applied(proposal: _Proposal) -> bool:
    """Whether the stored target already holds every after value: an earlier Apply committed it and its answer was lost."""
    target = proposal.target
    if proposal.kind == "character":
        stored: Mapping[str, Any] | None = await get_character_card(target["character_id"])
    elif proposal.kind == "lorebook_entry":
        entry = await get_lorebook_entry(target["entry_id"])
        stored = entry if entry is not None and entry["world_id"] == target["world_id"] else None
    else:
        stored = await get_user_persona(target["persona_id"])
    return stored is not None and _holds(stored, proposal.after)


async def _write_message(proposal: _Proposal) -> bool:
    """The message edit: admitted under its chat's lane, compared and written with every invariant in one transaction.

    ``False`` when the message already held the approved text, saved by another writer: nothing is written then, since saving it
    again would repeat the edit's invariants over newer, valid state.
    """
    cid, after, before = proposal.target["conversation_id"], proposal.after["content"], proposal.before["content"]
    # Apply is not a chat route, so the deletion fence on chat paths does not cover it; the admission does.
    try:
        async with idle_chat_admission(cid):
            original = await get_message_by_id(proposal.target["message_id"])
            if await get_conversation(cid) is None or original is None or original["conversation_id"] != cid:
                raise _Missing
            try:
                saved = await save_message_edit(original, after, expected=before)
            except StaleWrite:
                # Read inside the lane, so nothing has written it since. Holding the approved text, it was committed either by
                # an earlier attempt of this proposal, with its invariants in the same transaction, or by another writer.
                if (original["content"] or "") != after:
                    raise
                return proposal.uncertain
            except BaseException:
                proposal.uncertain = True
                raise
            if not saved:
                raise _Missing
            return True
    except ChatUnavailable as exc:
        # Nothing is recorded: either may pass, and the same proposal can be applied again.
        if exc.reason == "deleting":
            _refuse(409, "busy", "That chat is being deleted. Nothing was applied.")
        _refuse(409, "busy", "That chat is generating right now. Apply again once it has finished.")


async def _write(proposal: _Proposal) -> bool:
    """Compare the expected values and write the approved ones in one transaction, through each target's own write path.

    Returns once that transaction has committed, ``False`` when a message already held the approved text and nothing was
    written. A target's later steps are :func:`_follow_up`'s.
    """
    target, before, after = proposal.target, dict(proposal.before), dict(proposal.after)
    if proposal.kind == "message":
        return await _write_message(proposal)
    try:
        if proposal.kind == "character":
            written: object = await update_character_card(target["character_id"], after, expected=before)
        elif proposal.kind == "lorebook_entry":
            written = await update_lorebook_entry(target["entry_id"], after, expected=before, world_id=target["world_id"])
        else:
            written = await update_user_persona(target["persona_id"], after, expected=before)
    except StaleWrite:
        if not await _already_applied(proposal):
            raise
        return True
    if written is None:
        raise _Missing
    return True


async def _follow_up(proposal: _Proposal) -> None:
    """What a committed edit's own route does after its write; safe to repeat, and repeated by a retry until it succeeds."""
    if proposal.kind == "character":
        card_id = proposal.target["character_id"]
        card = await get_character_card(card_id)
        if card is not None:
            await sync_conversations_for_card(card_id, card)


async def _commit(proposal: _Proposal) -> dict[str, Any]:
    """Write the proposal and record how it ended, so a repeated Apply answers the same without writing again."""
    if not proposal.committed:
        try:
            proposal.matched = not await _write(proposal)
        except StaleWrite as exc:
            proposal.refusal = (
                409,
                {
                    "code": "stale",
                    "message": "That text changed after the preview, so nothing was applied. Ask again for a fresh preview.",
                    "fields": list(exc.fields),
                },
            )
        except _Missing:
            proposal.refusal = (404, {"code": "missing", "message": "What this proposal edits no longer exists."})
        if proposal.refusal is not None:
            _refuse(proposal.refusal[0], **proposal.refusal[1])
        # Committed: from here a failure is retried by repeating what follows, never by comparing the text again, and the
        # answer is kept for a whole expiry period from now.
        proposal.committed = True
        proposal.expires = _clock() + PROPOSAL_TTL_SECS
    await _follow_up(proposal)
    summary = (
        f"Nothing was written: {proposal.label} already held the approved text."
        if proposal.matched
        else f"Applied the edit to {proposal.label}."
    )
    proposal.result = {
        "ok": True,
        "proposal_id": proposal.id,
        "summary": summary,
        "written": not proposal.matched,
        "resources": [dict(proposal.target)],
    }
    return proposal.result


async def apply_proposal(proposal_id: str) -> dict[str, Any]:
    """Commit a proposal once. Concurrent and repeated Applies wait for the first and return its outcome."""
    proposal = get_proposal(proposal_id)
    async with proposal.lock:
        if proposal.result is not None:
            return {**proposal.result, "repeated": True}
        if proposal.refusal is not None:
            _refuse(proposal.refusal[0], **proposal.refusal[1])
        # Once admitted the write settles whole, through a dropped connection too: a cancelled await proves nothing about
        # whether SQLite committed.
        return await settled(_commit(proposal))


def continuable(proposal_id: str) -> _Proposal:
    """The applied proposal whose request may still be continued once; claim it with :func:`claim_continuation`."""
    proposal = get_proposal(proposal_id)
    if proposal.result is None:
        _refuse(409, "not_applied", "That edit has not been applied, so there is nothing to continue.")
    if proposal.continued:
        _refuse(409, "continued", "That request has already been continued once.")
    return proposal


def claim_continuation(proposal: _Proposal) -> bool:
    """Take the one continuation, once its run has been admitted; ``False`` when another run took it first."""
    if proposal.continued:
        return False
    proposal.continued = True
    return True


def release_continuation(proposal: _Proposal) -> None:
    """Give back a continuation whose run ended before doing anything, so it may run again."""
    proposal.continued = False


def approval_note(proposal: _Proposal) -> str:
    """The server's own record of the approval, for the continuation run; replayed history never supplies one."""
    fields = ", ".join(_FIELD_LABELS[name].lower() for name in proposal.after)
    return f"The user applied the proposed edit to the {fields} of {proposal.label} (proposal {proposal.id})."
