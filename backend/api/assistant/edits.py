"""The assistant's reviewed edits: four tools that propose a change to existing text and write nothing."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, create_model

from ...core.macros import resolve_inline
from ...database import get_character_card, get_lorebook_entry, get_message_by_id, get_user_persona, get_world, greeting_rerolls
from ...features.assistant import MAX_ITEM_CHARS, AssistantTool, ToolArguments, ToolFailure, ToolOutput, ToolSet, ViewContext
from ..deps import conversation_busy
from .proposals import FIELD_LABELS, PROPOSAL_TTL_SECS, Proposal, is_open, revise, store
from .targets import (
    CHARACTER_LIST_FIELDS,
    ID_CHARS,
    CharacterField,
    CharacterListField,
    character_in_view,
    chat_field,
    chat_in_view,
    quoted,
)

# What one argument may carry, and the longest field a whole-field rewrite may replace. Longer text is refused, never cut: a
# change to a field that long belongs in find_replace.
_EDIT_CHARS = MAX_ITEM_CHARS

# How a read marks the place it shortened a text. Edits refuse text carrying it: it is the reader's note, not the field's.
_CLIP_MARK = re.compile(r"…\[\+\d+ chars\]")

# A list field's bounds: as many items as a new card may carry, a tag as long as a new card's tag.
_LIST_ITEMS = 20
_TAG_CHARS = 60

_MODES = (
    "replace_all: `text` becomes the whole field, only for a field you have read whole. "
    "find_replace: `text` replaces `find`, copied exactly from the field, which must occur in it exactly once."
)


def _edited(before: str, args: Any, reread: str) -> str:
    """The field after *args*: a whole replacement, or one literal replacement of text that occurs exactly once.

    *reread* tells the model how to read this field whole, for a refusal that needs it to.
    """
    if _CLIP_MARK.search(args.text) and not _CLIP_MARK.search(before):
        raise ToolFailure(
            "The text contains a marker like …[+N chars]: a read puts it where it shortened a field, and it is not part of "
            f"the field. {reread} Then send only the field's real text. Nothing was proposed."
        )
    if args.mode == "replace_all":
        if args.find:
            raise ToolFailure("find is only for find_replace; with replace_all leave it empty. Nothing was proposed.")
        if len(before) > _EDIT_CHARS:
            raise ToolFailure(
                f"That field is {len(before)} characters, more than a whole-field rewrite may replace ({_EDIT_CHARS}). Use "
                f"find_replace on the part to change, once for each part. {reread} Nothing was proposed."
            )
        return args.text
    if not args.find:
        raise ToolFailure("find_replace needs the exact text to find, and find was empty. Nothing was proposed.")
    # Overlapping matches count: each is a place the edit could mean.
    first = before.find(args.find)
    if first == -1:
        raise ToolFailure(f"The find text does not occur in that field. {reread} Then copy the text exactly.")
    if before.find(args.find, first + 1) != -1:
        raise ToolFailure("The find text occurs more than once. Include more of the text around it so it matches once.")
    return before.replace(args.find, args.text, 1)


_FRESH = (
    "Nothing has changed yet. The user reviews every edit proposed in this request once it ends, then applies or cancels them; "
    "until then every read shows the stored text."
)

_STACKED = (
    "Added on top of the edit already proposed for this field in this request: the one proposal now holds both. Nothing has "
    "changed yet; every read still shows the stored text until the user applies."
)


def _preview(proposal: Proposal, *, stacked: bool) -> ToolOutput:
    """The tool's result for *proposal* as it now stands, and the preview the browser shows with Apply and Cancel."""
    before, after, name = proposal.before, proposal.after, proposal.name
    listed = isinstance(after, list)
    key, unit = ("items", "items") if listed else ("chars", "characters")
    preview = {
        "proposal_id": proposal.id,
        "kind": proposal.kind,
        "target": dict(proposal.target),
        "label": proposal.label,
        "fields": [{"field": name, "label": FIELD_LABELS[name], "before": before, "after": after}],
        "expires_in_s": PROPOSAL_TTL_SECS,
    }
    data = {
        "proposed": True,
        "applied": False,
        "proposal_id": proposal.id,
        "target": dict(proposal.target),
        "field": name,
        f"before_{key}": len(before),
        f"after_{key}": len(after),
        "note": _STACKED if stacked else _FRESH,
    }
    what = f"{FIELD_LABELS[name].lower()} of {proposal.label}"
    return ToolOutput(
        data,
        f"Proposed an edit to the {what} ({len(before):,} → {len(after):,} {unit}); nothing changes until it is applied.",
        {"proposal_id": proposal.id, **{key: value for key, value in proposal.target.items() if key != "kind"}},
        proposal=preview,
    )


_LIST_MODES = (
    " For tags and alternate_greetings, which are lists: add_items adds `items`, remove_items removes `items`, and "
    "replace_all makes `items` the whole new list."
)


def _edit_args(name: str, *, lists: bool = False, **target: Any) -> type[ToolArguments]:
    """An edit tool's arguments: what it targets, then how the text changes; *lists* adds the list fields' modes."""
    modes = ("replace_all", "find_replace", *(("add_items", "remove_items") if lists else ()))
    return create_model(
        name,
        __base__=ToolArguments,
        **target,
        mode=(Literal[modes], Field(description=_MODES + (_LIST_MODES if lists else ""))),
        text=(
            str,
            Field(
                max_length=_EDIT_CHARS,
                description="The new text; with find_replace, empty deletes `find`.",
            ),
        ),
        find=(str, Field(default="", max_length=_EDIT_CHARS, description="find_replace only.")),
    )


_CHAT = (str, chat_field())

_Item = Annotated[str, StringConstraints(min_length=1, max_length=_EDIT_CHARS)]

_CharacterEdit = _edit_args(
    "_CharacterEdit",
    lists=True,
    character_id=(
        str,
        Field(default="", max_length=ID_CHARS, description="Empty: the character in view."),
    ),
    field=(Literal[CharacterField, "name", CharacterListField], Field(description="The card field to edit.")),
    items=(
        list[_Item],
        Field(
            default_factory=list,
            max_length=_LIST_ITEMS,
            description="tags and alternate_greetings only: the items to add, to remove, or with replace_all the "
            "field's complete new list.",
        ),
    ),
)

_EntryEdit = _edit_args(
    "_EntryEdit",
    world_id=(str, Field(min_length=1, max_length=ID_CHARS, description="The entry's world, from list_worlds.")),
    entry_id=(int, Field(ge=1, description="The entry's id, from list_lorebook_entries.")),
    field=(Literal["name", "content"], Field(description="The entry field to edit.")),
)

_PersonaEdit = _edit_args(
    "_PersonaEdit",
    persona_id=(int, Field(ge=1, description="The persona's id, from list_personas.")),
    field=(Literal["name", "description"], Field(description="The persona field to edit.")),
)

_MessageEdit = _edit_args(
    "_MessageEdit", conversation_id=_CHAT, message_id=(int, Field(ge=1, description="The message's id, from read_messages."))
)

_GREETING_REROLLS = (
    "Message {id} is this chat's opening greeting, and it still holds random macros such as {{{{roll}}}} that roll again each "
    "time the chat is shown, until the user sends a first message. Its text changes on every view, so no preview of it could "
    "ever apply. Nothing was proposed. Tell the user to edit the greeting with the Edit button on it in the chat, which keeps "
    "the text as written; or, once they have sent a message, it stays fixed and can be edited here."
)

_UNDONE = (
    "That returns the field to its stored text, undoing the edit already proposed for it in this request. Nothing changed: "
    "that earlier edit is still proposed. If the user no longer wants it, tell them to Cancel."
)

_ON_PENDING = (
    "This field already has an edit proposed in this request, and a further edit works on the text with that edit made, not "
    "the stored text a read shows."
)


def _list_edit(args: Any) -> Callable[[list[str]], list[str]]:
    """How *args* change a list field, applied to its stored list or the list already proposed for it."""
    if args.mode == "find_replace" or args.text or args.find:
        raise ToolFailure(
            f"{args.field} is a list: use add_items, remove_items or replace_all with items, and leave text and find "
            "empty. Nothing was proposed."
        )
    items = _items(args)

    def edit(base: list[str]) -> list[str]:
        if args.mode == "replace_all":
            return items
        if args.mode == "add_items":
            return [*base, *(item for item in items if item not in base)]
        missing = [item for item in items if item not in base]
        if missing:
            raise ToolFailure(
                f"Not in {args.field}: {json.dumps(missing, ensure_ascii=False)}. It holds "
                f"{json.dumps(base, ensure_ascii=False)[:1000]}. Nothing was proposed."
            )
        return [item for item in base if item not in items]

    return edit


def _items(args: Any) -> list[str]:
    """*args*' items, checked and stripped; tags once each."""
    items = [item.strip() for item in args.items]
    if any(not item for item in items):
        raise ToolFailure("An item is blank. Nothing was proposed.")
    if any(_CLIP_MARK.search(item) for item in items):
        reread = 'get_character with fields ["alternate_greetings"] reads them whole.'
        raise ToolFailure(
            "An item contains a marker like …[+N chars], which a read puts where it shortened text. "
            f"{reread} Then send the items' real text. Nothing was proposed."
        )
    if args.field == "tags":
        if any(len(item) > _TAG_CHARS for item in items):
            raise ToolFailure(f"A tag is at most {_TAG_CHARS} characters. Nothing was proposed.")
        return list(dict.fromkeys(items))
    return items


def build_edit_tools(view: ViewContext) -> list[AssistantTool]:
    """The four edit tools, bound to the request's view.

    The run holds one proposal per target and field: a later edit to a field it already proposed an edit to is made on that
    proposal's pending text and updates the same proposal, so its preview always compares the stored text with the result of
    every edit the request made to it.
    """

    tools = ToolSet()
    pending: dict[str, Proposal] = {}

    def propose(
        target: dict[str, Any],
        label: str,
        name: str,
        stored: str | list[str],
        args: Any,
        reread: str,
        *,
        required: str = "",
        list_edit: Callable[[list[str]], list[str]] | None = None,
    ) -> ToolOutput:
        """Propose the edit *args* make to field *name* of *target*; *required* names a field that may not be left empty.

        A list field takes *list_edit*, which makes its new list from the stored one or the one already proposed.
        """
        kind = target["kind"]
        key = json.dumps([target, name], sort_keys=True)
        held = pending.get(key)
        if held is not None and not is_open(held):
            # Expired or forgotten meanwhile: this edit starts a new proposal from the stored text.
            held = None
        base = stored if held is None else held.after
        if list_edit is not None:
            assert isinstance(base, list)
            after: str | list[str] = list_edit(base)
        else:
            assert isinstance(base, str)
            after = _edited(base, args, reread if held is None else f"{_ON_PENDING} {reread}")
        if kind == "message" and isinstance(after, str):
            # Inline macros resolve once, here, as the edit route resolves them on save: Apply commits this exact text, so a
            # random macro cannot roll a second time and save something the preview did not show.
            after = resolve_inline(after)
        if required and isinstance(after, str) and not after.strip():
            raise ToolFailure(f"{required} cannot be left empty. Nothing was proposed.")
        if after == base:
            raise ToolFailure("That leaves the text exactly as it is, so there is nothing to propose.")
        if held is None:
            pending[key] = store(kind, target, label, name, stored, after)
            return _preview(pending[key], stacked=False)
        if after == held.before:
            raise ToolFailure(_UNDONE)
        revise(held, after)
        return _preview(held, stacked=True)

    @tools.add(
        "confirm",
        _CharacterEdit,
        "Propose an edit to a character: its name, a text field (description, personality, scenario, first_mes (first "
        "message), mes_example (example messages), creator_notes), or a list field (tags, alternate_greetings).",
    )
    async def edit_character(args: Any) -> ToolOutput:
        card_id = await character_in_view(args.character_id, view)
        card = await get_character_card(card_id)
        if card is None:
            raise ToolFailure(f"No character has the id {card_id}.")
        target = {"kind": "character", "character_id": card_id}
        label = f"character {quoted(card.get('name'))}"
        if args.field in CHARACTER_LIST_FIELDS:
            return propose(
                target,
                label,
                args.field,
                [str(item) for item in card.get(args.field) or []],
                args,
                "",
                list_edit=_list_edit(args),
            )
        if args.items or args.mode in ("add_items", "remove_items"):
            raise ToolFailure(
                f"items, add_items and remove_items are only for tags and alternate_greetings; {args.field} is text: use "
                "replace_all or find_replace. Nothing was proposed."
            )
        if args.field == "name":
            reread = "get_character shows the name."
            return propose(target, label, "name", str(card.get("name") or ""), args, reread, required="A character's name")
        reread = f'get_character with fields ["{args.field}"] reads it whole, one page per text_offset.'
        return propose(target, label, args.field, str(card.get(args.field) or ""), args, reread)

    @tools.add("confirm", _EntryEdit, "Propose an edit to a lore entry's name or content.")
    async def edit_lorebook_entry(args: Any) -> ToolOutput:
        world = await get_world(args.world_id)
        if world is None:
            raise ToolFailure(f"No world has the id {args.world_id}. list_worlds shows the ids.")
        entry = await get_lorebook_entry(args.entry_id)
        if entry is None or entry["world_id"] != world["id"]:
            raise ToolFailure(f"That world has no entry {args.entry_id}. list_lorebook_entries shows its entries.")
        reread = (
            "list_lorebook_entries with entry_id reads the entry whole: its name, and its content one page per text_offset."
        )
        target = {"kind": "lorebook_entry", "world_id": world["id"], "entry_id": entry["id"]}
        label = f"lore entry {quoted(entry['name'])} in {quoted(world['name'])}"
        required = "An entry's name" if args.field == "name" else ""
        return propose(target, label, args.field, str(entry[args.field] or ""), args, reread, required=required)

    @tools.add("confirm", _PersonaEdit, "Propose an edit to a persona's name or description.")
    async def edit_persona(args: Any) -> ToolOutput:
        persona = await get_user_persona(args.persona_id)
        if persona is None:
            raise ToolFailure(f"No persona has the id {args.persona_id}. list_personas shows the ids.")
        reread = (
            "list_personas with persona_id reads the persona whole: its name, and its description one page per text_offset."
        )
        target = {"kind": "persona", "persona_id": persona["id"]}
        label, before = f"persona {quoted(persona['name'])}", str(persona[args.field] or "")
        required = "A persona's name" if args.field == "name" else ""
        return propose(target, label, args.field, before, args, reread, required=required)

    @tools.add(
        "confirm",
        _MessageEdit,
        "Propose an edit to one chat message's text. Inline macros such as {{roll}} resolve in the preview.",
    )
    async def edit_message(args: Any) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=True)
        cid = conv["id"]
        if conversation_busy(cid):
            raise ToolFailure("That chat is generating right now. Ask again once it has finished.")
        message = await get_message_by_id(args.message_id)
        if message is None or message["conversation_id"] != cid:
            raise ToolFailure(f"That chat has no message {args.message_id}. read_messages shows the ids.")
        if await greeting_rerolls(cid, message["id"]):
            raise ToolFailure(_GREETING_REROLLS.format(id=message["id"]))
        reread = "read_messages with message_id reads that message whole, one page per text_offset."
        target = {"kind": "message", "conversation_id": cid, "message_id": message["id"]}
        label = f"message {message['id']} in {quoted(conv['title'])}"
        return propose(target, label, "content", message["content"] or "", args, reread, required="A message")

    return tools
