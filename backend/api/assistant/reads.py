"""The assistant's read tools: allowlisted projections over the existing readers, bounded and paged."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import Field

from ...core import extract_hyperparams
from ...core.settings import Settings
from ...database import (
    LIBRARY_VIEWS,
    get_active_lorebook_entries,
    get_active_path,
    get_document,
    get_documents,
    get_effective_world_ids,
    get_lorebook_entries,
    get_phrase_bank_rows,
    get_settings,
    get_speaker_names,
    get_user_personas,
    get_worlds,
    list_character_cards,
    run_library_query,
)
from ...database.models import ConversationRow
from ...features.assistant import (
    MAX_RESULT_CHARS,
    AssistantTool,
    NoArguments,
    ToolArguments,
    ToolFailure,
    ToolOutput,
    ToolSet,
    ViewContext,
)
from ...features.fragment_state import StateWriteError, state_panel
from ...features.lorebook import select_effective_entries
from ...pipeline import estimate_context_size
from .targets import (
    CHARACTER_FIELDS,
    ID_CHARS,
    CharacterField,
    cast_of,
    character_field,
    character_of,
    chat_field,
    chat_in_view,
    clip,
    entry_of,
    message_label,
    message_of,
    persona_of,
    quoted,
    world_of,
)
from .toggles import switch_summary

MAX_MESSAGES = 20
_MAX_LIST_ROWS = 30
_DEFAULT_LIST_ROWS = 20
_MESSAGE_CHARS = 1200
_FIELD_CHARS = 700
_STATE_ENTRY_CHARS = 600
_DESCRIPTION_CHARS = 2400
_PREVIEW_CHARS = 300
_DOCUMENT_CHARS = 5000
_QUERY_CHARS = 200
_SQL_CHARS = 4000
# A name a single-item read shows whole; a page of the item's text still fits beside it.
_WHOLE_NAME_CHARS = 1000
# What a whole encoded result may take: the loop refuses anything over its cap, so every result is measured as it will be sent.
_RESULT_BUDGET = MAX_RESULT_CHARS - 100
# Leaves room for the result's own keys inside that budget.
_ROWS_BUDGET = MAX_RESULT_CHARS - 400
_SQL_CAPS = {"max_rows": 50, "max_cell_chars": 500, "max_result_chars": _ROWS_BUDGET, "time_limit_s": 5.0}
_SAMPLER_KEYS = ("temperature", "top_p", "min_p", "top_k", "repetition_penalty", "max_tokens")
_VIEW_NOTES = (
    "Read-only SQLite views; one SELECT with a LIMIT per query: "
    + "; ".join(f"{view}({', '.join(columns)})" for view, columns in LIBRARY_VIEWS.items())
    + ". messages.role is 'user' or 'assistant'. messages holds every branch: a chat's kept branch is the parent_id chain "
    "from conversations.active_leaf_id. tags, alternate_greetings and keywords are JSON arrays (json_each). "
    "lorebook_entries spans every world; a disabled or archived entry is not in effect. Select id and "
    f"conversation_id with message findings; substr long text. At most {_SQL_CAPS['max_rows']} rows, "
    f"{_SQL_CAPS['max_cell_chars']} characters a cell. Documents, settings and state are not here."
)
_TEXT_OFFSET = "To read on in a whole text: the last page's next_offset."
# The longest page of one text a read returns.
FULL_FIELD_CHARS = 4000


class _ListArgs(ToolArguments):
    query: str = Field(default="", max_length=_QUERY_CHARS, description="Case-insensitive text to match; empty lists all.")
    limit: int = Field(
        default=_DEFAULT_LIST_ROWS, ge=1, le=_MAX_LIST_ROWS, description=f"Rows to return, at most {_MAX_LIST_ROWS}."
    )
    offset: int = Field(default=0, ge=0, description="Rows to skip, for the next page.")


class _CharacterArgs(ToolArguments):
    character_id: str = character_field()
    fields: list[Literal[CharacterField, "name", "tags", "alternate_greetings"]] = Field(
        default_factory=list,
        max_length=len(CHARACTER_FIELDS) + 3,
        description="Text fields to read; empty reads all of them, shortened to fit. Name one to read it whole, in pages. "
        "name and tags come with every read. alternate_greetings, named alone, reads every alternate greeting whole.",
    )
    text_offset: int = Field(default=0, ge=0, description=_TEXT_OFFSET)


class _SqlArgs(ToolArguments):
    sql: str = Field(min_length=1, max_length=_SQL_CHARS)


class _MessagesArgs(ToolArguments):
    conversation_id: str = chat_field()
    count: int = Field(default=MAX_MESSAGES, ge=1, le=MAX_MESSAGES, description=f"Messages to read, at most {MAX_MESSAGES}.")
    before_message_id: int = Field(
        default=0, ge=0, description="Read the messages before this one on the kept branch: the last page's earlier_from."
    )
    query: str = Field(
        default="", max_length=_QUERY_CHARS, description="Only messages on the kept branch containing this text, any case."
    )
    message_id: int = Field(default=0, ge=0, description="One message to read whole, in pages, instead of the latest ones.")
    number: int = Field(default=0, ge=0, description="The same, by the message's #N on the kept branch.")
    text_offset: int = Field(default=0, ge=0, description=_TEXT_OFFSET)


class _ChatListArgs(_ListArgs):
    conversation_id: str = chat_field()


class _StateArgs(ToolArguments):
    conversation_id: str = chat_field()


class _EntriesArgs(_ListArgs):
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The world's id, from list_worlds.")
    entry_id: int = Field(default=0, ge=0, description="One entry whose content to read whole, in pages, instead of a list.")
    text_offset: int = Field(default=0, ge=0, description=_TEXT_OFFSET)


class _PersonaArgs(_ListArgs):
    persona_id: int = Field(
        default=0, ge=0, description="One persona whose description to read whole, in pages, instead of a list."
    )
    text_offset: int = Field(default=0, ge=0, description=_TEXT_OFFSET)


class _DocumentArgs(ToolArguments):
    document_id: str = Field(default="", max_length=ID_CHARS, description="The document's id; empty lists documents.")
    query: str = Field(default="", max_length=_QUERY_CHARS, description="When listing: text to match in titles.")
    offset: int = Field(
        default=0, ge=0, description="When reading: the character position to start from. When listing: rows to skip."
    )


def _size(value: object) -> int:
    """The characters *value* takes as the loop encodes it, escapes included."""
    return len(json.dumps(value, ensure_ascii=False))


def _matches(query: str, *texts: object) -> bool:
    needle = query.strip().casefold()
    return not needle or any(needle in str(text or "").casefold() for text in texts)


def _page(rows: Sequence[Mapping[str, Any]], args: _ListArgs) -> dict[str, Any]:
    """One bounded page of projected *rows*, with what a caller needs to ask for the rest."""
    page: list[Mapping[str, Any]] = []
    size = 0
    for row in rows[args.offset : args.offset + args.limit]:
        size += len(json.dumps(row, ensure_ascii=False)) + 1
        if page and size > _ROWS_BUDGET:
            break
        page.append(row)
    shown = args.offset + len(page)
    return {
        "total": len(rows),
        "truncated": shown < len(rows),
        "next_offset": shown if shown < len(rows) else None,
        "rows": page,
    }


def _noting(items: Sequence[tuple[dict[str, Any], str]], key: str, args: _ListArgs, note: str) -> dict[str, Any]:
    """A page of rows that each clip one text under *key*, with *note* when one on the page was clipped."""
    texts = {row["id"]: text for row, text in items}
    listed = _page([row for row, _ in items], args)
    if any(row[key] != texts[row["id"]] for row in listed["rows"]):
        listed["note"] = note
    return listed


def _listed(result: Mapping[str, Any], what: str) -> ToolOutput:
    rows = result["rows"]
    more = f" of {result['total']}" if result["truncated"] else ""
    return ToolOutput(result, f"Listed {len(rows)}{more} {what}.", {"ids": [row["id"] for row in rows]})


def _text_page(
    text: str,
    offset: int,
    head: Mapping[str, Any],
    key: str,
    *,
    length: int = FULL_FIELD_CHARS,
    offset_key: str = "text_offset",
) -> dict[str, Any]:
    """One page of *text* from *offset*, under *key* after *head*, cut only where a page ends and never past the result cap.

    Every key a reader pages on comes before the text, so nothing it relies on can be cut. A field no longer than
    ``FULL_FIELD_CHARS`` fits one page unless its escapes overrun it; the next page then starts where this one ends.
    """
    if offset and offset >= len(text):
        raise ToolFailure(f"{offset_key} {offset} is past the end of that text, which is {len(text)} characters.")

    def page(length: int) -> dict[str, Any]:
        end = min(offset + length, len(text))
        more = end < len(text)
        return {
            **head,
            offset_key: offset,
            "total_chars": len(text),
            "truncated": more,
            "next_offset": end if more else None,
            key: text[offset:end],
        }

    data = page(length)
    while (excess := _size(data) - _RESULT_BUDGET) > 0 and length:
        length = max(0, min(length, len(data[key])) - excess)
        data = page(length)
    return data


def build_read_tools(settings: Settings, view: ViewContext) -> list[AssistantTool]:
    """The read tools, in their stable order, bound to the request's settings and view."""
    tools = ToolSet()

    @tools.add(
        "read",
        NoArguments,
        "What the user is looking at: the chat's title, kept branch, cast, worlds in effect and context size, "
        "or the open document or character.",
    )
    async def get_context(_args: NoArguments) -> ToolOutput:
        data: dict[str, Any] = {"mode": view.mode}
        ids: dict[str, Any] = {}
        if view.document_id:
            data["document"] = {"id": view.document_id, "title": clip(view.document_title, _QUERY_CHARS)}
            ids["document_id"] = view.document_id
        if view.character_id:
            data["character"] = {"id": view.character_id, "name": clip(view.character_name, _QUERY_CHARS)}
            ids["character_id"] = view.character_id
        if view.conversation_id:
            conv = await chat_in_view("", view, changes=False)
            size = await estimate_context_size(conv)
            worlds = {world["id"]: world["name"] for world in await get_worlds()}
            data["chat"] = {
                "id": conv["id"],
                "title": clip(conv["title"], _QUERY_CHARS),
                "kind": conv.get("kind") or "solo",
                "active_leaf_id": conv.get("active_leaf_id"),
                "cast": await cast_of(conv),
                "effective_worlds": [
                    {"id": world_id, "name": clip(worlds.get(world_id), _QUERY_CHARS)}
                    for world_id in await get_effective_world_ids(conv["id"])
                ],
                "context_size": {
                    "messages": size["message_count"],
                    "tokens_est": size["total_tokens_est"],
                    "estimate_kind": size["estimate_kind"],
                    "tokens_by_part": {part: value["tokens_est"] for part, value in size["breakdown"].items()},
                },
            }
            ids["conversation_id"] = conv["id"]
        return ToolOutput(data, f"Read the {view.mode} view.", ids)

    @tools.add("read", _ListArgs, "Search character cards by name, creator or tag.")
    async def list_characters(args: _ListArgs) -> ToolOutput:
        rows = [
            {
                "id": card.get("id"),
                "name": clip(card.get("name"), _QUERY_CHARS),
                "tags": [clip(tag, 60) for tag in (card.get("tags") or [])[:12]],
                "creator": clip(card.get("creator"), 80),
                "creator_notes": clip(card.get("creator_notes"), _PREVIEW_CHARS),
            }
            for card in await list_character_cards()
            if _matches(args.query, card.get("name"), card.get("creator"), *(card.get("tags") or []))
        ]
        return _listed(_page(rows, args), "characters")

    @tools.add("read", _CharacterArgs, "Read one character's authored text. A group chat has several characters, so name one.")
    async def get_character(args: _CharacterArgs) -> ToolOutput:
        card = await character_of(args.character_id, view)
        card_id = card.get("id")
        if "alternate_greetings" in args.fields:
            return greetings(card, args)
        # The name and tags head every read, so naming only them reads no text field.
        named = [name for name in dict.fromkeys(args.fields) if name not in ("name", "tags")]
        chosen = named if args.fields else list(CHARACTER_FIELDS)
        texts = {name: str(card.get(name) or "") for name in chosen}
        if args.text_offset and len(chosen) != 1:
            raise ToolFailure("text_offset pages through one field: name exactly one in fields.")
        meta = {
            "id": card.get("id"),
            "name": clip(card.get("name"), _QUERY_CHARS),
            "creator": clip(card.get("creator"), 80),
            "tags": [clip(tag, 60) for tag in (card.get("tags") or [])[:20]],
            "alternate_greeting_count": len(card.get("alternate_greetings") or []),
            "field_chars": {name: len(text) for name, text in texts.items()},
        }
        if not chosen:
            return ToolOutput(meta, f"Read the name and tags of character {meta['name']}.", {"character_id": card_id})
        # Each field's share before measuring; named fields split the whole result between them.
        if args.fields:
            caps = dict.fromkeys(chosen, _RESULT_BUDGET // len(chosen))
        else:
            caps = {name: _DESCRIPTION_CHARS if name == "description" else _FIELD_CHARS for name in chosen}

        if len(chosen) == 1 and args.fields:
            # One field named: read whole, a page at a time, never clipped.
            data = _text_page(texts[chosen[0]], args.text_offset, {**meta, "field": chosen[0]}, chosen[0])
            return ToolOutput(data, f"Read the {chosen[0]} of character {meta['name']}.", {"character_id": card_id})

        def build() -> dict[str, Any]:
            clipped = [name for name in chosen if len(texts[name]) > caps[name]]
            note = (
                {"note": "Clipped fields end with …[+N chars]. Name one field to read it whole, a page per text_offset."}
                if clipped
                else {}
            )
            return {**meta, "clipped": clipped, **note, **{name: clip(texts[name], caps[name]) for name in chosen}}

        # Measured as encoded: quotes and newlines take two characters each there, so a raw count overruns the cap.
        data = build()
        while (excess := _size(data) - _RESULT_BUDGET) > 0:
            widest = max(chosen, key=lambda name: _size(data[name]))
            if not caps[widest]:
                break
            caps[widest] = max(0, min(caps[widest], len(texts[widest])) - max(excess, 32))
            data = build()
        return ToolOutput(data, f"Read character {meta['name']}.", {"character_id": card_id})

    def greetings(card: Mapping[str, Any], args: _CharacterArgs) -> ToolOutput:
        """Every alternate greeting whole, since an edit replaces the list whole; one too long to send is refused, not cut."""
        if len(args.fields) != 1 or args.text_offset:
            raise ToolFailure("alternate_greetings is read on its own and whole: name it alone, with no text_offset.")
        items = [str(item) for item in card.get("alternate_greetings") or []]
        data = {"id": card.get("id"), "name": clip(card.get("name"), _QUERY_CHARS), "alternate_greetings": items}
        if _size(data) > _RESULT_BUDGET:
            raise ToolFailure(
                f"Its {len(items)} alternate greetings are too long to read whole here, so they cannot be edited here either. "
                "The character editor shows them."
            )
        return ToolOutput(
            data, f"Read {len(items)} alternate greetings of character {data['name']}.", {"character_id": card["id"]}
        )

    @tools.add("read", _SqlArgs, f"Search the library with SQL; the way to find or list chats. {_VIEW_NOTES}")
    async def query_library(args: _SqlArgs) -> ToolOutput:
        result = await run_library_query(args.sql, **_SQL_CAPS)
        if "error" in result:
            raise ToolFailure(f"The query failed: {clip(result['error'], _PREVIEW_CHARS)}")
        rows = result.get("rows") or []
        more = " (more rows exist)" if result.get("more_rows") else ""
        data = {
            "note": "Rows may come from branches the user left: say so when a finding might. Only read_messages follows the "
            "kept branch.",
            **result,
        }
        # The reader keeps its first row whatever its width; one that wide cannot be sent, so ask for a narrower query.
        if _size(data) > _RESULT_BUDGET:
            width = "Even one row" if len(rows) == 1 else "The result"
            raise ToolFailure(
                f"{width} is wider than the {MAX_RESULT_CHARS}-character result limit. "
                "Select fewer columns, or use substr(column, 1, 200) on long text."
            )
        return ToolOutput(data, f"Query returned {len(rows)} rows{more}.")

    @tools.add(
        "read",
        _MessagesArgs,
        f"Read messages on a chat's kept branch, at most {MAX_MESSAGES}: the latest, those before before_message_id, or "
        "those containing query. With message_id or number, read one message whole. A message's number is the #N the chat window "
        "shows; name messages to the user by it, never by id.",
    )
    async def read_messages(args: _MessagesArgs) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=False)
        path = await get_active_path(conv["id"])
        speakers = await get_speaker_names(conv["id"]) if conv.get("kind") == "group" else {}
        if args.number:
            if args.message_id or args.number > len(path):
                raise ToolFailure(f"number is a #N from 1 to {len(path)} on the kept branch, given instead of message_id.")
            args = args.model_copy(update={"message_id": path[args.number - 1]["id"]})
        if args.message_id:
            if args.before_message_id or args.query:
                raise ToolFailure("message_id or number reads one message: leave before_message_id and query empty.")
            return await read_message(conv, path, speakers, args)
        if args.text_offset:
            raise ToolFailure("text_offset pages through one message: name its message_id.")
        window = path
        if args.before_message_id:
            index = next((i for i, row in enumerate(path) if row["id"] == args.before_message_id), None)
            if index is None:
                raise ToolFailure(
                    f"Message {args.before_message_id} is not on the chat's kept branch. Use an id read_messages returned."
                )
            window = path[:index]
        needle = args.query.strip().casefold()
        candidates = [row for row in window if needle in (row["content"] or "").casefold()] if needle else window
        numbers = {row["id"]: index + 1 for index, row in enumerate(path)}
        rows: list[dict[str, Any]] = []
        size = 0
        for message in reversed(candidates[-args.count :]):
            speaker = speakers.get(message.get("speaker_member_id") or "")
            row = {
                "id": message["id"],
                "number": numbers[message["id"]],
                "role": message["role"],
                **({"speaker": clip(speaker, _QUERY_CHARS)} if speaker else {}),
                "created_at": message["created_at"],
                "content": clip(message["content"], _MESSAGE_CHARS),
            }
            size += len(json.dumps(row, ensure_ascii=False)) + 1
            if rows and size > _ROWS_BUDGET:
                break
            rows.append(row)
        rows.reverse()
        earlier = len(candidates) - len(rows)
        data: dict[str, Any] = {
            "conversation_id": conv["id"],
            "title": clip(conv["title"], _QUERY_CHARS),
            "messages": rows,
            "path_length": len(path),
            "truncated": earlier > 0,
            # Where the next page back starts: pass it as before_message_id.
            "earlier_from": rows[0]["id"] if earlier > 0 and rows else None,
        }
        if needle:
            data["matches"] = len(candidates)
        shown = {row["id"] for row in rows}
        if any(len(message["content"] or "") > _MESSAGE_CHARS for message in candidates if message["id"] in shown):
            data["note"] = "A message ending with …[+N chars] was shortened; read it whole with its message_id."
        ids = {"conversation_id": conv["id"], "message_ids": [row["id"] for row in rows]}
        if needle:
            summary = (
                f"Found {len(candidates)} messages on the kept branch containing {json.dumps(args.query)}; read {len(rows)}."
            )
        elif args.before_message_id:
            summary = f"Read {len(rows)} messages before {message_label(path, args.before_message_id)}."
        else:
            summary = f"Read the last {len(rows)} of {len(path)} messages on the kept branch."
        return ToolOutput(data, summary, ids)

    async def read_message(
        conv: ConversationRow, path: Sequence[Mapping[str, Any]], speakers: Mapping[str, str], args: _MessagesArgs
    ) -> ToolOutput:
        """One message of the chat, kept branch or not, read whole a page at a time."""
        message = await message_of(conv["id"], args.message_id)
        speaker = speakers.get(message.get("speaker_member_id") or "")
        number = next((index + 1 for index, row in enumerate(path) if row["id"] == message["id"]), None)
        head = {
            "conversation_id": conv["id"],
            "id": message["id"],
            **({"number": number} if number else {}),
            "role": message["role"],
            **({"speaker": clip(speaker, _QUERY_CHARS)} if speaker else {}),
            "on_kept_branch": number is not None,
        }
        data = _text_page(message["content"] or "", args.text_offset, head, "content")
        ids = {"conversation_id": conv["id"], "message_ids": [message["id"]]}
        label = message_label(path, message["id"])
        return ToolOutput(data, f"Read {label} from character {args.text_offset}.", ids)

    @tools.add("read", _ListArgs, "List worlds (lorebooks) by name.")
    async def list_worlds(args: _ListArgs) -> ToolOutput:
        rows = [
            {
                "id": world["id"],
                "name": clip(world["name"], _QUERY_CHARS),
                "is_global": bool(world["is_global"]),
                "dynamic_enabled": bool(world["dynamic_enabled"]),
            }
            for world in await get_worlds()
            if _matches(args.query, world["name"])
        ]
        return _listed(_page(rows, args), "worlds")

    @tools.add(
        "read",
        _EntriesArgs,
        "List one world's entries with their text, or read one entry's content whole; query matches names, keywords "
        "and content.",
    )
    async def list_lorebook_entries(args: _EntriesArgs) -> ToolOutput:
        world = await world_of(args.world_id)
        world_head = {"id": world["id"], "name": clip(world["name"], _QUERY_CHARS)}
        if args.entry_id:
            _, entry = await entry_of(args.world_id, args.entry_id)
            head = {"world": world_head, "id": entry["id"], "name": clip(entry["name"], _WHOLE_NAME_CHARS)}
            data = _text_page(entry["content"] or "", args.text_offset, head, "content")
            return ToolOutput(data, f"Read lore entry {entry['id']}.", {"world_id": world["id"], "entry_id": entry["id"]})
        if args.text_offset:
            raise ToolFailure("text_offset pages through one entry: name its entry_id.")
        items = [
            (
                {
                    "id": entry["id"],
                    "name": clip(entry["name"], _QUERY_CHARS),
                    "keywords": [clip(keyword, 60) for keyword in (entry["keywords"] or [])[:12]],
                    "enabled": bool(entry["enabled"]),
                    "constant": bool(entry["constant"]),
                    "layer": entry["entry_layer"],
                    **({"archived": True} if entry["archived"] else {}),
                    "content": clip(entry["content"], _FIELD_CHARS),
                },
                entry["content"] or "",
            )
            for entry in await get_lorebook_entries(args.world_id)
            if _matches(args.query, entry["name"], entry["content"], *(entry["keywords"] or []))
        ]
        note = "Content ending with …[+N chars] was shortened; read it whole with the entry's entry_id."
        return _listed(
            {"world": world_head, **_noting(items, "content", args, note)},
            f"entries in {world_head['name']}",
        )

    @tools.add(
        "read",
        _ChatListArgs,
        "List the worlds a chat uses and the lore entries in effect there: their enabled entries, with their text. It cannot "
        "say which entries a given reply drew on; nothing records that.",
    )
    async def list_active_lore(args: _ChatListArgs) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=False)
        world_ids = await get_effective_world_ids(conv["id"])
        # What the prompt draws from: dynamic replacements and suppressions resolved, as the chat's own lore view shows it.
        entries = select_effective_entries(await get_active_lorebook_entries(world_ids))
        items = [
            (
                {
                    "id": entry["id"],
                    "world": {"id": entry["world_id"], "name": clip(entry.get("world_name"), _QUERY_CHARS)},
                    "name": clip(entry["name"], _QUERY_CHARS),
                    "keywords": [clip(keyword, 60) for keyword in (entry["keywords"] or [])[:12]],
                    "constant": bool(entry["constant"]),
                    "content": clip(entry["content"], _FIELD_CHARS),
                },
                entry["content"] or "",
            )
            for entry in entries
            if _matches(args.query, entry["name"], entry["content"], *(entry["keywords"] or []))
        ]
        note = "Content ending with …[+N chars] was shortened; list_lorebook_entries with its world_id and entry_id reads it whole."
        names = {world["id"]: world["name"] for world in await get_worlds()}
        listed = {
            "conversation_id": conv["id"],
            "worlds_in_effect": [{"id": world_id, "name": clip(names.get(world_id), _QUERY_CHARS)} for world_id in world_ids],
            "note_matching": "A keyword entry is offered when its keywords come up in the chat; a constant one always is.",
            **_noting(items, "content", args, note),
        }
        return _listed(listed, f"lore entries in effect for {quoted(conv['title'])}")

    @tools.add("read", _PersonaArgs, "List the user's personas, or read one's description whole.")
    async def list_personas(args: _PersonaArgs) -> ToolOutput:
        active = settings.get("active_persona_id")
        if args.persona_id:
            persona = await persona_of(args.persona_id)
            head = {"id": persona["id"], "name": clip(persona["name"], _WHOLE_NAME_CHARS), "active": persona["id"] == active}
            data = _text_page(persona["description"] or "", args.text_offset, head, "description")
            return ToolOutput(data, f"Read persona {persona['id']}.", {"persona_id": persona["id"]})
        if args.text_offset:
            raise ToolFailure("text_offset pages through one persona: name its persona_id.")
        items = [
            (
                {
                    "id": persona["id"],
                    "name": clip(persona["name"], _QUERY_CHARS),
                    "active": persona["id"] == active,
                    "description": clip(persona["description"], _FIELD_CHARS),
                },
                persona["description"] or "",
            )
            for persona in await get_user_personas()
            if _matches(args.query, persona["name"], persona["description"])
        ]
        note = "A description ending with …[+N chars] was shortened; read it whole with its persona_id."
        return _listed(_noting(items, "description", args, note), "personas")

    @tools.add(
        "read",
        _DocumentArgs,
        "Without document_id, list documents; with one, read its saved text. Unsaved editor text is never included.",
    )
    async def read_document(args: _DocumentArgs) -> ToolOutput:
        if not args.document_id:
            rows = [
                {"id": document["id"], "title": clip(document["title"], _QUERY_CHARS), "updated_at": document["updated_at"]}
                for document in await get_documents()
                if _matches(args.query, document["title"])
            ]
            return _listed(_page(rows, _ListArgs(limit=_MAX_LIST_ROWS, offset=args.offset)), "documents")
        document = await get_document(args.document_id)
        if document is None:
            raise ToolFailure(f"No document has the id {args.document_id}.")
        content = document["content"] or ""
        head = {
            "id": document["id"],
            "title": clip(document["title"], _QUERY_CHARS),
            "updated_at": document["updated_at"],
            "note": "This is the saved text. Edits still unsaved in the editor are not included.",
        }
        data = _text_page(content, args.offset, head, "content", length=_DOCUMENT_CHARS, offset_key="offset")
        summary = f"Read {len(data['content'])} of {len(content)} saved characters of {data['title']}."
        return ToolOutput(data, summary, {"document_id": document["id"]})

    @tools.add(
        "read",
        _StateArgs,
        "Read a chat's scene state: what each state fragment holds on the kept branch, as the Inspector's State tab shows it.",
    )
    async def read_state(args: _StateArgs) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=False)
        try:
            panel = await state_panel(conv["id"])
        except StateWriteError as exc:
            raise ToolFailure(str(exc)) from None
        cap = _STATE_ENTRY_CHARS

        def build() -> dict[str, Any]:
            return {
                "conversation_id": conv["id"],
                "has_state": panel["has_state"],
                "fragments": [
                    {
                        "fragment_id": fragment["fragment_id"],
                        "label": clip(fragment["label"], _QUERY_CHARS),
                        "mode": fragment["mode"],
                        "enabled": fragment["enabled"],
                        **({} if fragment["configured"] else {"note": "Its fragment was removed; this is saved state."}),
                        "entries": [
                            {"text": clip(entry["text"], cap), "turn_index": entry["turn_index"]}
                            for entry in fragment["entries"]
                        ],
                    }
                    for fragment in panel["fragments"]
                ],
            }

        data = build()
        while _size(data) > _RESULT_BUDGET and cap > 40:
            cap //= 2
            data = build()
        if _size(data) > _RESULT_BUDGET:
            raise ToolFailure("That chat's state is too large to read at once; the Inspector's State tab shows it.")
        entries = sum(len(fragment["entries"]) for fragment in data["fragments"])
        summary = f"Read {len(data['fragments'])} state fragments ({entries} entries) of {quoted(conv['title'])}."
        return ToolOutput(data, summary, {"conversation_id": conv["id"]})

    @tools.add("read", NoArguments, "Pipeline switches and sampler values. No endpoints, models or credentials.")
    async def get_settings_summary(_args: NoArguments) -> ToolOutput:
        # Read now: a toggle earlier in this request has changed what the request's own snapshot says.
        current = await get_settings()
        data = {
            "switches": switch_summary(current),
            "samplers": {
                "writer": {key: value for key, value in extract_hyperparams(current).items() if key in _SAMPLER_KEYS},
                "agent": {
                    key: value for key, value in extract_hyperparams(current, lane="agent").items() if key in _SAMPLER_KEYS
                },
            },
        }
        return ToolOutput(data, "Read the pipeline switches and sampler values.")

    @tools.add("read", _ListArgs, "List the phrase bank: the phrase groups the Output Auditor removes from replies.")
    async def list_banned_phrases(args: _ListArgs) -> ToolOutput:
        rows = [
            {
                "id": row["id"],
                "kind": row["kind"],
                "variants": [clip(variant, _QUERY_CHARS) for variant in row["variants"][:12]],
                **({"pattern": clip(row["pattern"], _QUERY_CHARS)} if row["pattern"] else {}),
            }
            for row in await get_phrase_bank_rows()
            if _matches(args.query, row["pattern"], *row["variants"])
        ]
        return _listed(_page(rows, args), "phrase groups")

    return tools
