"""The assistant's reviewed edits: six tools that propose a change to existing text and write nothing.

A field an earlier request already proposed an edit to, and the user has not applied, is edited on from that pending text: the
new proposal replaces the earlier one on the user's card.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Any, Literal

from pydantic import Field, StringConstraints, create_model

from ...core.macros import resolve_inline
from ...database import get_active_path, greeting_rerolls
from ...features.assistant import MAX_ITEM_CHARS, AssistantTool, ToolArguments, ToolFailure, ToolOutput, ToolSet, ViewContext
from .fragments import MOOD_TEXT_FIELDS, TEXT_CAPS, check_field_applies, fragment_text, fragment_text_problem
from .proposals import Proposal, change_label, is_open, preview, revise, store
from .targets import (
    CHARACTER_LIST_FIELDS,
    CharacterField,
    CharacterListField,
    character_field,
    character_of,
    chat_field,
    chat_in_view,
    entry_field,
    entry_of,
    fragment_field,
    interactive_fragment_of,
    message_label,
    message_of,
    mood_fragment_of,
    persona_of,
    quoted,
    world_field,
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
    "find_replace: `text` replaces `find`, copied exactly from the field, which must occur in it exactly once. "
    "replace_every: the same, for every occurrence of `find`."
)
_ESCAPES = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/"}


def _unescaped(text: str) -> str:
    """*text* with JSON string escapes decoded: a read shows a field JSON-encoded, and a copy of it as shown carries them."""
    return re.sub(r'\\(["\\/nrt])', lambda match: _ESCAPES[match[1]], text)


def _edited(before: str, args: Any, reread: str) -> str:
    """The field after *args*: a whole replacement, or a literal replacement of text that occurs once, or of each occurrence.

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
        raise ToolFailure(f"{args.mode} needs the exact text to find, and find was empty. Nothing was proposed.")
    find, text = args.find, args.text
    if find not in before:
        find, text = _unescaped(find), _unescaped(text)
        if find not in before:
            raise ToolFailure(f"The find text does not occur in that field. {reread} Then copy the text exactly.")
    if args.mode == "replace_every":
        return before.replace(find, text)
    # Overlapping matches count: each is a place the edit could mean.
    if before.find(find, before.find(find) + 1) != -1:
        raise ToolFailure(
            "The find text occurs more than once. Include more of the text around it so it matches once, or use "
            "replace_every to change every occurrence."
        )
    return before.replace(find, text, 1)


_FRESH = (
    "Nothing has changed yet. The user reviews every change proposed in this request once it ends, then applies or cancels "
    "each; until then every read shows the stored text."
)

_STACKED = (
    "Added on top of the edit already proposed for this field: the one proposal now holds both. Nothing has changed yet; every "
    "read still shows the stored text until the user applies."
)

_STALE_EARLIER = (
    "An earlier request proposed an edit to this field, but the stored text has changed since, so that preview could never "
    "apply. This edit starts from the stored text and replaces it. Nothing has changed yet."
)


def _preview(proposal: Proposal, note: str) -> ToolOutput:
    """The tool's result for *proposal* as it now stands, and the preview the browser shows with Apply and Cancel."""
    before, after, name = proposal.before, proposal.after, proposal.name
    listed = isinstance(after, list)
    key, unit = ("items", "items") if listed else ("chars", "characters")
    data = {
        "proposed": True,
        "applied": False,
        "proposal_id": proposal.id,
        "target": dict(proposal.target),
        "field": name,
        f"before_{key}": len(before),
        f"after_{key}": len(after),
        "note": note,
    }
    return ToolOutput(
        data,
        f"Proposed an edit to {change_label(proposal)} ({len(before):,} → {len(after):,} {unit}); nothing changes until it "
        "is applied.",
        {"proposal_id": proposal.id, **{key: value for key, value in proposal.target.items() if key != "kind"}},
        proposal=preview(proposal),
    )


_LIST_MODES = (
    " For tags and alternate_greetings, which are lists: add_items adds `items`, remove_items removes `items`, and "
    "replace_all makes `items` the whole new list."
)


def _edit_args(name: str, *, lists: bool = False, **target: Any) -> type[ToolArguments]:
    """An edit tool's arguments: what it targets, then how the text changes; *lists* adds the list fields' modes.

    A list edit carries no text, so with *lists* the text may be left out.
    """
    modes = ("replace_all", "find_replace", "replace_every", *(("add_items", "remove_items") if lists else ()))
    return create_model(
        name,
        __base__=ToolArguments,
        **target,
        mode=(Literal[modes], Field(description=_MODES + (_LIST_MODES if lists else ""))),
        text=(
            str,
            Field(
                "" if lists else ...,
                max_length=_EDIT_CHARS,
                description="The new text; replacing `find`, empty deletes it.",
            ),
        ),
        find=(str, Field(default="", max_length=_EDIT_CHARS, description="find_replace and replace_every only.")),
    )


_CHAT = (str, chat_field())

_Item = Annotated[str, StringConstraints(min_length=1, max_length=_EDIT_CHARS)]

_CharacterEdit = _edit_args(
    "_CharacterEdit",
    lists=True,
    character_id=(str, character_field()),
    field=(Literal[CharacterField, "name", CharacterListField], ...),
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
    world_id=(str, world_field()),
    entry_id=(int, entry_field()),
    field=(Literal["name", "content"], ...),
)

_PersonaEdit = _edit_args(
    "_PersonaEdit",
    persona_id=(int, Field(ge=1, description="The persona's id, from list_personas.")),
    field=(Literal["name", "description"], ...),
)

_MessageEdit = _edit_args(
    "_MessageEdit", conversation_id=_CHAT, message_id=(int, Field(ge=1, description="The message's id, from read_messages."))
)

_MoodFragmentEdit = _edit_args("_MoodFragmentEdit", fragment_id=(str, fragment_field()), field=(Literal[MOOD_TEXT_FIELDS], ...))

_InteractiveFragmentEdit = _edit_args(
    "_InteractiveFragmentEdit",
    fragment_id=(str, fragment_field()),
    field=(
        Literal[
            "label",
            "description",
            "injection_label",
            "gate_question",
            "question",
            "situation",
            "outcome_meaning",
            "outcome_guidance",
        ],
        ...,
    ),
    outcome=(str, Field(default="", max_length=64, description="outcome_meaning and outcome_guidance: which outcome.")),
)

_GREETING_REROLLS = (
    "Message {id} is this chat's opening greeting, and it still holds random macros such as {{{{roll}}}} that roll again each "
    "time the chat is shown, until the user sends a first message. Its text changes on every view, so no preview of it could "
    "ever apply. Nothing was proposed. Tell the user to edit the greeting with the Edit button on it in the chat, which keeps "
    "the text as written; or, once they have sent a message, it stays fixed and can be edited here."
)

_UNDONE = (
    "That returns the field to its stored text, undoing the edit already proposed for it. Nothing changed: that edit is still "
    "proposed. If the user no longer wants it, tell them to Cancel it on the card."
)

_ON_PENDING = (
    "This field already has an edit proposed in this request, and a further edit works on the text with that edit made, not "
    "the stored text a read shows."
)


def _list_edit(args: Any) -> Callable[[list[str]], list[str]]:
    """How *args* change a list field, applied to its stored list or the list already proposed for it."""
    if args.mode in ("find_replace", "replace_every") or args.text or args.find:
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
        reread = 'read_character with fields ["alternate_greetings"] reads them whole.'
        raise ToolFailure(
            "An item contains a marker like …[+N chars], which a read puts where it shortened text. "
            f"{reread} Then send the items' real text. Nothing was proposed."
        )
    if args.field == "tags":
        if any(len(item) > _TAG_CHARS for item in items):
            raise ToolFailure(f"A tag is at most {_TAG_CHARS} characters. Nothing was proposed.")
        return list(dict.fromkeys(items))
    return items


def proposal_key(target: Mapping[str, Any], name: str) -> str:
    """The one slot a target's field (or its delete) holds among the proposals a request knows of."""
    return json.dumps([dict(target), name], sort_keys=True)


def build_edit_tools(view: ViewContext, held: Sequence[Proposal] = ()) -> list[AssistantTool]:
    """The four edit tools, bound to the request's view and to *held*, the pending edits earlier requests proposed.

    The run holds one proposal per target and field: a later edit to a field it already proposed an edit to is made on that
    proposal's pending text and updates the same proposal, so its preview always compares the stored text with the result of
    every edit the request made to it. A field with a held edit is edited on from that edit's pending text, into a new proposal
    that replaces it.
    """

    tools = ToolSet()
    pending: dict[str, Proposal] = {proposal_key(item.target, item.name): item for item in held if item.action == "edit"}
    earlier = {item.id for item in held}

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
        check: Callable[[str], str] | None = None,
    ) -> ToolOutput:
        """Propose the edit *args* make to field *name* of *target*; *required* names a field that may not be left empty.

        A list field takes *list_edit*, which makes its new list from the stored one or the one already proposed. *check*
        says why a new text cannot be stored, or nothing.
        """
        kind = target["kind"]
        key = proposal_key(target, name)
        held = pending.get(key)
        if held is not None and not is_open(held):
            # Applied, expired or forgotten meanwhile: this edit starts a new proposal from the stored text.
            held = None
        inherited = held is not None and held.id in earlier
        note = _STACKED if held is not None else _FRESH
        if inherited and held is not None and held.before != stored:
            # The stored text moved under an earlier request's preview, which can never apply: start from what is stored.
            base, note = stored, _STALE_EARLIER
        else:
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
        if check is not None and isinstance(after, str) and (problem := check(after)):
            raise ToolFailure(f"{problem} Nothing was proposed.")
        if after == base:
            raise ToolFailure("That leaves the text exactly as it is, so there is nothing to propose.")
        if held is not None and after == stored:
            raise ToolFailure(_UNDONE)
        if held is None or inherited:
            pending[key] = store(kind, target, label, name, stored, after, replaces=held)
            return _preview(pending[key], note)
        revise(held, after)
        return _preview(held, note)

    @tools.add(
        "confirm",
        _CharacterEdit,
        "Propose an edit to a character: its name, a text field (description, personality, scenario, first_mes (first "
        "message), mes_example (example messages), creator_notes), or a list field (tags, alternate_greetings).",
    )
    async def edit_character(args: Any) -> ToolOutput:
        card = await character_of(args.character_id, view)
        target = {"kind": "character", "character_id": card.get("id")}
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
                "replace_all, find_replace or replace_every. Nothing was proposed."
            )
        if args.field == "name":
            reread = "read_character shows the name."
            return propose(target, label, "name", str(card.get("name") or ""), args, reread, required="A character's name")
        reread = f'read_character with fields ["{args.field}"] reads it whole, one page per text_offset.'
        return propose(target, label, args.field, str(card.get(args.field) or ""), args, reread)

    @tools.add("confirm", _EntryEdit, "Propose an edit to a lore entry's name or content.")
    async def edit_lore_entry(args: Any) -> ToolOutput:
        world, entry = await entry_of(args.world_id, args.entry_id, view)
        reread = "list_lore_entries with entry_id reads the entry whole: its name, and its content one page per text_offset."
        target = {"kind": "lorebook_entry", "world_id": world["id"], "entry_id": entry["id"]}
        label = f"lore entry {quoted(entry['name'])} in {quoted(world['name'])}"
        required = "An entry's name" if args.field == "name" else ""
        return propose(target, label, args.field, str(entry[args.field] or ""), args, reread, required=required)

    @tools.add("confirm", _PersonaEdit, "Propose an edit to a persona's name or description.")
    async def edit_persona(args: Any) -> ToolOutput:
        persona = await persona_of(args.persona_id)
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
        conv = await chat_in_view(args.conversation_id, view, changes=True, idle=True)
        cid = conv["id"]
        message = await message_of(cid, args.message_id)
        if await greeting_rerolls(cid, message["id"]):
            raise ToolFailure(_GREETING_REROLLS.format(id=message["id"]))
        reread = "read_messages with message_id reads that message whole, one page per text_offset."
        target = {"kind": "message", "conversation_id": cid, "message_id": message["id"]}
        label = f"{message_label(await get_active_path(cid), message['id'])} in {quoted(conv['title'])}"
        return propose(target, label, "content", message["content"] or "", args, reread, required="A message")

    @tools.add(
        "confirm", _MoodFragmentEdit, "Propose an edit to a mood fragment's label, description, prompt text or negative prompt."
    )
    async def edit_mood_fragment(args: Any) -> ToolOutput:
        fragment = await mood_fragment_of(args.fragment_id)
        reread = f'read_fragment with kind "mood", its fragment_id and field "{args.field}" reads it whole.'
        target = {"kind": "mood_fragment", "fragment_id": fragment["id"]}
        label = f"mood fragment {quoted(fragment['label'])}"
        required = {"label": "A fragment's label", "prompt_text": "A mood's prompt text"}.get(args.field, "")
        cap = TEXT_CAPS[args.field]

        def check(text: str) -> str:
            return f"That is {len(text):,} characters; a {args.field} may be at most {cap:,}." if len(text) > cap else ""

        stored = str(fragment[args.field] or "")
        return propose(target, label, args.field, stored, args, reread, required=required, check=check)

    @tools.add(
        "confirm",
        _InteractiveFragmentEdit,
        "Propose an edit to an interactive fragment's text: label, description, injection_label; a post_processing "
        "fragment's gate_question; a decision's question, situation, or one outcome's meaning (what it means, for the "
        "Judge) or guidance (what the story does on it).",
    )
    async def edit_interactive_fragment(args: Any) -> ToolOutput:
        fragment = await interactive_fragment_of(args.fragment_id)
        check_field_applies(fragment, args.field, args.outcome)
        target: dict[str, Any] = {
            "kind": "interactive_fragment",
            "fragment_id": fragment["id"],
            **({"outcome": args.outcome} if args.outcome else {}),
        }
        label = f"fragment {quoted(fragment['label'])}" + (f", outcome {quoted(args.outcome)}" if args.outcome else "")
        outcome = f', outcome "{args.outcome}"' if args.outcome else ""
        reread = f'read_fragment with kind "interactive", its fragment_id and field "{args.field}"{outcome} reads it whole.'
        stored = fragment_text(fragment, args.field, args.outcome) or ""

        def check(text: str) -> str:
            return fragment_text_problem(fragment, args.field, args.outcome, text)

        return propose(target, label, args.field, stored, args, reread, check=check)

    return tools
