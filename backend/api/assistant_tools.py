"""The assistant's tools: target checks, allowlisted projections, and bounds over the existing readers."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, TypeVar, get_args

from pydantic import Field, StringConstraints, create_model, field_validator

from ..core import extract_hyperparams
from ..core.settings import Settings
from ..database import (
    LIBRARY_VIEWS,
    create_lorebook_entry,
    get_active_path,
    get_character_card,
    get_document,
    get_documents,
    get_effective_world_ids,
    get_lorebook_entries,
    get_lorebook_entry,
    get_message_by_id,
    get_settings,
    get_speaker_names,
    get_user_persona,
    get_user_personas,
    get_world,
    get_worlds,
    list_character_cards,
    run_library_query,
    set_enabled_tool,
    update_settings,
)
from ..database.models import ConversationRow
from ..features.assistant import (
    MAX_RESULT_CHARS,
    AssistantTool,
    NoArguments,
    ToolArguments,
    ToolFailure,
    ToolOutput,
    ViewContext,
)
from ..pipeline import agent_enabled, estimate_context_size
from ..workflows import gpu_swap_host, list_workflows, set_workflow_toggle
from .assistant_actions import build_action_tools
from .assistant_edits import (
    FULL_FIELD_CHARS,
    ID_CHARS,
    Origin,
    ReadRecord,
    build_edit_tools,
    cast_of,
    character_in_view,
    chat_in_view,
    clip,
    not_blank,
)
from .assistant_media import MediaRun, build_media_tools
from .schemas import LorebookEntryCreate

_Args = TypeVar("_Args", bound=ToolArguments)

MAX_MESSAGES = 20
_MAX_LIST_ROWS = 30
_DEFAULT_LIST_ROWS = 20
_MESSAGE_CHARS = 1200
_FIELD_CHARS = 700
_DESCRIPTION_CHARS = 2400
_PREVIEW_CHARS = 300
_DOCUMENT_CHARS = 5000
_QUERY_CHARS = 200
_SQL_CHARS = 4000
_LORE_NAME_CHARS = 200
# A name a single-item read shows whole, so a rename may replace it whole; a page of the item's text still fits beside it.
_WHOLE_NAME_CHARS = 1000
_LORE_CONTENT_CHARS = 4000
_LORE_KEYWORD_CHARS = 100
_LORE_KEYWORDS = 20
# What a whole encoded result may take: the loop refuses anything over its cap, so every result is measured as it will be sent.
_RESULT_BUDGET = MAX_RESULT_CHARS - 100
# Leaves room for the result's own keys inside that budget.
_ROWS_BUDGET = MAX_RESULT_CHARS - 400
_SQL_CAPS = {"max_rows": 50, "max_cell_chars": 500, "max_result_chars": _ROWS_BUDGET, "time_limit_s": 5.0}

# A backstop only: projections are the privacy boundary, and an aliased column or an error string carries no key name. It
# matches whole secret names within a key's words, and any key whose last word is token, key, auth or secret, so ``api_key``,
# ``authToken``, ``hf_token`` or ``card_source_auth`` is scrubbed and ``tokens_est`` or ``max_tokens`` is not.
_SECRET_KEY = re.compile(
    r"^(?:token|cookies?)$|_(?:token|key|auth|secret)$|"
    r"(?:^|_)(?:api_?key|(?:access|auth|refresh|bearer|session|api|id)_?token|password|passwd|secret|"
    r"authorization|credentials?|cookies?)(?:_|$)"
)
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# The authored card text a reader may see. Prompt overrides, extensions, avatars and workflow state stay out.
_CharacterField = Literal["description", "personality", "scenario", "first_mes", "mes_example", "creator_notes"]
_CHARACTER_FIELDS: tuple[_CharacterField, ...] = get_args(_CharacterField)
_SAMPLER_KEYS = ("temperature", "top_p", "min_p", "top_k", "repetition_penalty", "max_tokens")

_VIEW_NOTES = (
    "Read-only SQLite views; one SELECT with a LIMIT per query: "
    + "; ".join(f"{view}({', '.join(columns)})" for view, columns in LIBRARY_VIEWS.items())
    + ". messages.role is 'user' or 'assistant'. messages holds every branch: a chat's kept branch is the parent_id chain "
    "from conversations.active_leaf_id. tags and alternate_greetings are JSON arrays (json_each). Select id and "
    "conversation_id with message findings; substr long text."
)


_TEXT_OFFSET = "When reading one text whole: the character position to start from, the last page's next_offset."


class _ListArgs(ToolArguments):
    query: str = Field(default="", max_length=_QUERY_CHARS, description="Case-insensitive text to match; empty lists all.")
    limit: int = Field(
        default=_DEFAULT_LIST_ROWS, ge=1, le=_MAX_LIST_ROWS, description=f"Rows to return, at most {_MAX_LIST_ROWS}."
    )
    offset: int = Field(default=0, ge=0, description="Rows to skip, for the next page.")


class _CharacterArgs(ToolArguments):
    character_id: str = Field(
        default="", max_length=ID_CHARS, description="The character's id; empty means the character in view."
    )
    fields: list[_CharacterField] = Field(
        default_factory=list,
        max_length=len(_CHARACTER_FIELDS),
        description="Text fields to read; empty reads all of them, shortened to fit. Name one to read it whole, in pages.",
    )
    text_offset: int = Field(default=0, ge=0, description=_TEXT_OFFSET)


class _SqlArgs(ToolArguments):
    sql: str = Field(min_length=1, max_length=_SQL_CHARS, description="One SELECT over the documented views.")


class _MessagesArgs(ToolArguments):
    conversation_id: str = Field(default="", max_length=ID_CHARS, description="The chat's id; empty means the chat in view.")
    count: int = Field(default=MAX_MESSAGES, ge=1, le=MAX_MESSAGES, description=f"Latest messages, at most {MAX_MESSAGES}.")
    message_id: int = Field(default=0, ge=0, description="One message to read whole, in pages, instead of the latest ones.")
    text_offset: int = Field(default=0, ge=0, description=_TEXT_OFFSET)


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


_Keyword = Annotated[str, StringConstraints(min_length=1, max_length=_LORE_KEYWORD_CHARS)]


class _LoreEntryArgs(ToolArguments):
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The world (lorebook) to add the entry to.")
    name: str = Field(min_length=1, max_length=_LORE_NAME_CHARS, description="The entry's title.")
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


def _size(value: object) -> int:
    """The characters *value* takes as the loop encodes it, escapes included."""
    return len(json.dumps(value, ensure_ascii=False))


def _secret_key(key: object) -> bool:
    words = re.sub(r"[\s-]+", "_", _CAMEL.sub("_", str(key).strip())).lower()
    return _SECRET_KEY.search(words) is not None


def _scrub(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: "[redacted]" if _secret_key(key) else _scrub(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(item) for item in value]
    return value


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


def _listed(result: Mapping[str, Any], what: str) -> ToolOutput:
    rows = result["rows"]
    more = f" of {result['total']}" if result["truncated"] else ""
    return ToolOutput(result, f"Listed {len(rows)}{more} {what}.", {"ids": [row["id"] for row in rows]})


def _text_page(text: str, offset: int, head: Mapping[str, Any], key: str) -> dict[str, Any]:
    """One page of *text* from *offset*, under *key* after *head*, cut only where a page ends and never past the result cap.

    Every key a reader pages on comes before the text, so nothing it relies on can be cut. A field no longer than
    ``FULL_FIELD_CHARS`` fits one page unless its escapes overrun it; the next page then starts where this one ends.
    """
    if offset and offset >= len(text):
        raise ToolFailure(f"text_offset {offset} is past the end of that text, which is {len(text)} characters.")

    def page(length: int) -> dict[str, Any]:
        body = text[offset : offset + length]
        end = offset + len(body)
        more = end < len(text)
        return {
            **head,
            "text_offset": offset,
            "total_chars": len(text),
            "truncated": more,
            "next_offset": end if more else None,
            key: body,
        }

    length = FULL_FIELD_CHARS
    data = page(length)
    while (excess := _size(data) - _RESULT_BUDGET) > 0 and length:
        length = max(0, min(length, len(data[key])) - excess)
        data = page(length)
    return data


def _read_tool(
    name: str, description: str, arguments: type[_Args], run: Callable[[_Args], Awaitable[ToolOutput]]
) -> AssistantTool:
    async def scrubbed(args: _Args) -> ToolOutput:
        output = await run(args)
        return ToolOutput(_scrub(output.data), output.summary, output.ids)

    return AssistantTool(name, description, arguments, "read", scrubbed)


def _gated(stored: object, master: bool) -> dict[str, bool] | bool:
    """A stored switch, or both values when it is stored on but a master switch above it is off."""
    return bool(stored) if master or not stored else {"stored": True, "effective": False}


def _switches(settings: Settings) -> dict[str, Any]:
    """Pipeline switches as stored, with the effective value beside any that a master switch overrides."""
    agent_on = agent_enabled(settings)
    workflows_on = bool(settings.get("workflows_globally_enabled", 1))
    tools = settings.get("enabled_tools") or {}
    # The audit checks run only inside the Output Auditor, which runs only with the Agent on.
    audit_on = agent_on and bool(tools.get("editor_apply_patch"))
    stored_workflows = settings.get("workflow_enabled")
    workflow_map = stored_workflows if isinstance(stored_workflows, dict) else {}
    return {
        "main_workflows_enabled": agent_on,
        "tools": {name: _gated(on, agent_on) for name, on in tools.items()},
        "length_guard": {
            "enabled": _gated(settings.get("length_guard_enabled"), agent_on),
            "enforce": bool(settings.get("length_guard_enforce")),
            "max_words": settings.get("length_guard_max_words"),
            "max_paragraphs": settings.get("length_guard_max_paragraphs"),
        },
        "agentic_lorebook": _gated(settings.get("agentic_lorebook_enabled"), agent_on),
        # Splits Director and state-update calls per fragment; both run only with the Agent on.
        "director_individual_fragments": _gated(settings.get("director_individual_fragments"), agent_on),
        "prevent_prompt_overrides": bool(settings.get("prevent_prompt_overrides")),
        "editor_audit_checks": {
            name: _gated(on, audit_on) for name, on in (settings.get("editor_audit_toggles") or {}).items()
        },
        "secondary_workflows_enabled": workflows_on,
        "workflows": {workflow.id: _gated(workflow_map.get(workflow.id, True), workflows_on) for workflow in list_workflows()},
    }


@dataclass(frozen=True)
class _Switch:
    """One stored pipeline switch: its label, how to read and write it, the settings key it lives in, and its master."""

    label: str
    read: Callable[[Settings], bool]
    write: Callable[[bool], Awaitable[object]]
    key: str
    gate: str | None


def _flag(key: str, default: int = 0) -> Callable[[Settings], bool]:
    return lambda settings: bool(settings.get(key, default))


def _set_flag(key: str) -> Callable[[bool], Awaitable[object]]:
    # The settings route's own writer: one column, so a concurrent change to any other key is untouched.
    return lambda on: update_settings({key: int(on)})


def _set_workflows_flag(on: bool) -> Awaitable[object]:
    async def write() -> None:
        await update_settings({"workflows_globally_enabled": int(on)})
        # The GPU swap plan is off while workflows are, so a render right after switching them on must not read a cached "off".
        gpu_swap_host.invalidate()

    return write()


def _tool_flag(tool: str) -> Callable[[Settings], bool]:
    return lambda settings: bool((settings.get("enabled_tools") or {}).get(tool, False))


def _set_tool_flag(tool: str) -> Callable[[bool], Awaitable[object]]:
    # One key of the map, so a concurrent change to another tool's flag is kept.
    return lambda on: set_enabled_tool(tool, on)


def _workflow_flag(workflow_id: str) -> Callable[[Settings], bool]:
    def read(settings: Settings) -> bool:
        stored = settings.get("workflow_enabled")
        return bool((stored if isinstance(stored, dict) else {}).get(workflow_id, True))

    return read


def _set_workflow_flag(workflow_id: str) -> Callable[[bool], Awaitable[object]]:
    # The workflow route's own path, runtime callbacks included.
    return lambda on: set_workflow_toggle(workflow_id, on)


def _switch_table() -> dict[str, _Switch]:
    """Every toggle name with one stored switch behind it, in a stable order; workflow names follow the registry."""
    table = {
        "agent": _Switch("Agent", _flag("enable_agent", 1), _set_flag("enable_agent"), "enable_agent", None),
        "director": _Switch("Director", _tool_flag("direct_scene"), _set_tool_flag("direct_scene"), "enabled_tools", "agent"),
        "director_individual_fragments": _Switch(
            "Individual fragment processing",
            _flag("director_individual_fragments"),
            _set_flag("director_individual_fragments"),
            "director_individual_fragments",
            "agent",
        ),
        "output_auditor": _Switch(
            "Output Auditor", _tool_flag("editor_apply_patch"), _set_tool_flag("editor_apply_patch"), "enabled_tools", "agent"
        ),
        "length_guard": _Switch(
            "Length Guard", _flag("length_guard_enabled"), _set_flag("length_guard_enabled"), "length_guard_enabled", "agent"
        ),
        "agentic_lorebook": _Switch(
            "Agentic Lorebook",
            _flag("agentic_lorebook_enabled"),
            _set_flag("agentic_lorebook_enabled"),
            "agentic_lorebook_enabled",
            "agent",
        ),
        "workflows": _Switch(
            "Secondary workflows",
            _flag("workflows_globally_enabled", 1),
            _set_workflows_flag,
            "workflows_globally_enabled",
            None,
        ),
    }
    for workflow in list_workflows():
        table[f"workflow:{workflow.id}"] = _Switch(
            f"{workflow.display_name} workflow",
            _workflow_flag(workflow.id),
            _set_workflow_flag(workflow.id),
            "workflow_enabled",
            "workflows",
        )
    return table


# "Editor" names the Editor pass, which the Output Auditor and the Length Guard each start. Turning it off turns both off;
# turning it on turns on the Output Auditor alone, since the Length Guard is an optional limit the user sets separately.
_EDITOR_OFF = ("output_auditor", "length_guard")
_EDITOR_ON = ("output_auditor",)


def _toggle_switches(toggle: str, on: bool) -> tuple[str, ...]:
    return (_EDITOR_ON if on else _EDITOR_OFF) if toggle == "editor" else (toggle,)


def _effective(name: str, table: Mapping[str, _Switch], settings: Settings) -> bool:
    switch = table[name]
    return switch.read(settings) and (switch.gate is None or _effective(switch.gate, table, settings))


def _toggle_description(table: Mapping[str, _Switch]) -> str:
    return (
        "Turn one pipeline switch on or off: the state the user asked for, never a flip. "
        f"Switches: editor, {', '.join(table)}. "
        "editor is the Editor pass: off turns off output_auditor and length_guard, on turns on output_auditor only. "
        "agent is the master of the Director, Output Auditor, Length Guard, Agentic Lorebook, state updates, feedback and "
        "post-processing; workflows (secondary workflows) is the master of each workflow:<id>. A master that is off keeps "
        "the switches under it from taking effect; it is reported, not changed."
    )


def _wire(view: ViewContext) -> dict[str, Any]:
    ids = {"conversation_id": view.conversation_id, "character_id": view.character_id, "document_id": view.document_id}
    return {"mode": view.mode, **{key: value for key, value in ids.items() if value}}


def build_tools(
    settings: Settings, view: ViewContext, origin: Origin | None = None, *, media: MediaRun | None = None
) -> list[AssistantTool]:
    """Every assistant tool, in their stable order, bound to the view the request was sent from.

    *origin* is the request an edit proposal remembers, so its continuation after Apply answers the same request in the same
    view. The tools share one record of the texts this run has read whole, which a whole-field edit needs. *media* links the
    media tools to the run's Stop and progress; without it they are left out.
    """
    record = ReadRecord()

    def paged(kind: str, resource: object, name: str, text: str, data: Mapping[str, Any], key: str) -> None:
        start = data["text_offset"]
        record.page(kind, resource, name, text, start, start + len(data[key]))

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

    async def list_characters(args: _ListArgs) -> ToolOutput:
        cards = {
            card.get("id"): card
            for card in await list_character_cards()
            if _matches(args.query, card.get("name"), card.get("creator"), *(card.get("tags") or []))
        }
        rows = [
            {
                "id": card_id,
                "name": clip(card.get("name"), _QUERY_CHARS),
                "tags": [clip(tag, 60) for tag in (card.get("tags") or [])[:12]],
                "creator": clip(card.get("creator"), 80),
                "creator_notes": clip(card.get("creator_notes"), _PREVIEW_CHARS),
            }
            for card_id, card in cards.items()
        ]
        listed = _page(rows, args)
        for row in listed["rows"]:
            notes = str(cards[row["id"]].get("creator_notes") or "")
            record.shown("character", row["id"], "creator_notes", notes, row["creator_notes"])
        return _listed(listed, "characters")

    async def get_character(args: _CharacterArgs) -> ToolOutput:
        card_id = await character_in_view(args.character_id, view)
        card = await get_character_card(card_id)
        if card is None:
            raise ToolFailure(f"No character has the id {card_id}.")
        chosen = list(dict.fromkeys(args.fields)) or list(_CHARACTER_FIELDS)
        texts = {name: str(card.get(name) or "") for name in chosen}
        if args.text_offset and len(chosen) != 1:
            raise ToolFailure("text_offset pages through one field: name exactly one in fields.")
        # Each field's share before measuring; named fields split the whole result between them.
        if args.fields:
            caps = dict.fromkeys(chosen, _RESULT_BUDGET // len(chosen))
        else:
            caps = {name: _DESCRIPTION_CHARS if name == "description" else _FIELD_CHARS for name in chosen}
        meta = {
            "id": card.get("id"),
            "name": clip(card.get("name"), _QUERY_CHARS),
            "creator": clip(card.get("creator"), 80),
            "tags": [clip(tag, 60) for tag in (card.get("tags") or [])[:20]],
            "alternate_greeting_count": len(card.get("alternate_greetings") or []),
            "field_chars": {name: len(text) for name, text in texts.items()},
        }

        if len(chosen) == 1 and args.fields:
            # One field named: read whole, a page at a time, never clipped.
            data = _text_page(texts[chosen[0]], args.text_offset, {**meta, "field": chosen[0]}, chosen[0])
            paged("character", card_id, chosen[0], texts[chosen[0]], data, chosen[0])
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
        for name in chosen:
            record.shown("character", card_id, name, texts[name], data[name])
        return ToolOutput(data, f"Read character {meta['name']}.", {"character_id": card_id})

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

    async def read_messages(args: _MessagesArgs) -> ToolOutput:
        conv = await chat_in_view(args.conversation_id, view, changes=False)
        path = await get_active_path(conv["id"])
        speakers = await get_speaker_names(conv["id"]) if conv.get("kind") == "group" else {}
        if args.message_id:
            return await read_message(conv, path, speakers, args)
        if args.text_offset:
            raise ToolFailure("text_offset pages through one message: name its message_id.")
        rows: list[dict[str, Any]] = []
        size = 0
        for message in reversed(path[-args.count :]):
            speaker = speakers.get(message.get("speaker_member_id") or "")
            row = {
                "id": message["id"],
                "role": message["role"],
                **({"speaker": clip(speaker, _QUERY_CHARS)} if speaker else {}),
                "turn_index": message["turn_index"],
                "created_at": message["created_at"],
                "content": clip(message["content"], _MESSAGE_CHARS),
            }
            size += len(json.dumps(row, ensure_ascii=False)) + 1
            if rows and size > _ROWS_BUDGET:
                break
            rows.append(row)
            record.shown("message", message["id"], "content", message["content"] or "", row["content"])
        rows.reverse()
        data = {
            "conversation_id": conv["id"],
            "title": clip(conv["title"], _QUERY_CHARS),
            "messages": rows,
            "path_length": len(path),
            "truncated": len(rows) < len(path),
        }
        if any(len(message["content"] or "") > _MESSAGE_CHARS for message in path[len(path) - len(rows) :]):
            data["note"] = "A message ending with …[+N chars] was shortened; read it whole with its message_id."
        ids = {"conversation_id": conv["id"], "message_ids": [row["id"] for row in rows]}
        return ToolOutput(data, f"Read the last {len(rows)} of {len(path)} messages on the kept branch.", ids)

    async def read_message(
        conv: ConversationRow, path: Sequence[Mapping[str, Any]], speakers: Mapping[str, str], args: _MessagesArgs
    ) -> ToolOutput:
        """One message of the chat, kept branch or not, read whole a page at a time."""
        message = await get_message_by_id(args.message_id)
        if message is None or message["conversation_id"] != conv["id"]:
            raise ToolFailure(f"That chat has no message {args.message_id}. read_messages without message_id shows the ids.")
        speaker = speakers.get(message.get("speaker_member_id") or "")
        head = {
            "conversation_id": conv["id"],
            "id": message["id"],
            "role": message["role"],
            **({"speaker": clip(speaker, _QUERY_CHARS)} if speaker else {}),
            "on_kept_branch": any(row["id"] == message["id"] for row in path),
        }
        data = _text_page(message["content"] or "", args.text_offset, head, "content")
        paged("message", message["id"], "content", message["content"] or "", data, "content")
        ids = {"conversation_id": conv["id"], "message_ids": [message["id"]]}
        return ToolOutput(data, f"Read message {message['id']} from character {args.text_offset}.", ids)

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

    async def list_lorebook_entries(args: _EntriesArgs) -> ToolOutput:
        world = await get_world(args.world_id)
        if world is None:
            raise ToolFailure(f"No world has the id {args.world_id}. list_worlds shows the ids.")
        world_head = {"id": world["id"], "name": clip(world["name"], _QUERY_CHARS)}
        if args.entry_id:
            entry = await get_lorebook_entry(args.entry_id)
            if entry is None or entry["world_id"] != world["id"]:
                raise ToolFailure(f"That world has no entry {args.entry_id}. list_lorebook_entries shows its entries.")
            head = {"world": world_head, "id": entry["id"], "name": clip(entry["name"], _WHOLE_NAME_CHARS)}
            record.shown("lorebook_entry", entry["id"], "name", entry["name"] or "", head["name"])
            data = _text_page(entry["content"] or "", args.text_offset, head, "content")
            paged("lorebook_entry", entry["id"], "content", entry["content"] or "", data, "content")
            return ToolOutput(data, f"Read lore entry {entry['id']}.", {"world_id": world["id"], "entry_id": entry["id"]})
        if args.text_offset:
            raise ToolFailure("text_offset pages through one entry: name its entry_id.")
        contents: dict[int, str] = {}
        rows = []
        for entry in await get_lorebook_entries(args.world_id):
            if not _matches(args.query, entry["name"], entry["content"], *(entry["keywords"] or [])):
                continue
            contents[entry["id"]] = entry["content"] or ""
            rows.append(
                {
                    "id": entry["id"],
                    "name": clip(entry["name"], _QUERY_CHARS),
                    "keywords": [clip(keyword, 60) for keyword in (entry["keywords"] or [])[:12]],
                    "enabled": bool(entry["enabled"]),
                    "constant": bool(entry["constant"]),
                    "layer": entry["entry_layer"],
                    **({"archived": True} if entry["archived"] else {}),
                    "content": clip(entry["content"], _FIELD_CHARS),
                }
            )
        result = {"world": world_head, **_page(rows, args)}
        for row in result["rows"]:
            record.shown("lorebook_entry", row["id"], "content", contents[row["id"]], row["content"])
        if any(len(row["content"]) > _FIELD_CHARS for row in result["rows"]):
            result["note"] = "Content ending with …[+N chars] was shortened; read it whole with the entry's entry_id."
        return _listed(result, f"entries in {result['world']['name']}")

    async def list_personas(args: _PersonaArgs) -> ToolOutput:
        active = settings.get("active_persona_id")
        if args.persona_id:
            persona = await get_user_persona(args.persona_id)
            if persona is None:
                raise ToolFailure(f"No persona has the id {args.persona_id}. list_personas shows the ids.")
            head = {"id": persona["id"], "name": clip(persona["name"], _WHOLE_NAME_CHARS), "active": persona["id"] == active}
            record.shown("persona", persona["id"], "name", persona["name"] or "", head["name"])
            data = _text_page(persona["description"] or "", args.text_offset, head, "description")
            paged("persona", persona["id"], "description", persona["description"] or "", data, "description")
            return ToolOutput(data, f"Read persona {persona['id']}.", {"persona_id": persona["id"]})
        if args.text_offset:
            raise ToolFailure("text_offset pages through one persona: name its persona_id.")
        descriptions: dict[int, str] = {}
        rows = []
        for persona in await get_user_personas():
            if not _matches(args.query, persona["name"], persona["description"]):
                continue
            descriptions[persona["id"]] = persona["description"] or ""
            rows.append(
                {
                    "id": persona["id"],
                    "name": clip(persona["name"], _QUERY_CHARS),
                    "active": persona["id"] == active,
                    "description": clip(persona["description"], _FIELD_CHARS),
                }
            )
        listed = _page(rows, args)
        for row in listed["rows"]:
            record.shown("persona", row["id"], "description", descriptions[row["id"]], row["description"])
        if any(len(row["description"]) > _FIELD_CHARS for row in listed["rows"]):
            listed["note"] = "A description ending with …[+N chars] was shortened; read it whole with its persona_id."
        return _listed(listed, "personas")

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

        def page(length: int) -> dict[str, Any]:
            # Every key the reader needs to page on comes before the body, so nothing it relies on can be cut.
            body = content[args.offset : args.offset + length]
            end = args.offset + len(body)
            return {
                "id": document["id"],
                "title": clip(document["title"], _QUERY_CHARS),
                "updated_at": document["updated_at"],
                "offset": args.offset,
                "total_chars": len(content),
                "truncated": end < len(content),
                "next_offset": end if end < len(content) else None,
                "note": "This is the saved text. Edits still unsaved in the editor are not included.",
                "content": body,
            }

        length = _DOCUMENT_CHARS
        data = page(length)
        while (excess := _size(data) - _RESULT_BUDGET) > 0 and length:
            length = max(0, min(length, len(data["content"])) - excess)
            data = page(length)
        return ToolOutput(
            data,
            f"Read {len(data['content'])} of {len(content)} saved characters of {data['title']}.",
            {"document_id": document["id"]},
        )

    async def get_settings_summary(_args: NoArguments) -> ToolOutput:
        # Read now: a toggle earlier in this request has changed what the request's own snapshot says.
        current = await get_settings()
        data = {
            "switches": _switches(current),
            "samplers": {
                "writer": {key: value for key, value in extract_hyperparams(current).items() if key in _SAMPLER_KEYS},
                "agent": {
                    key: value for key, value in extract_hyperparams(current, lane="agent").items() if key in _SAMPLER_KEYS
                },
            },
        }
        return ToolOutput(data, "Read the pipeline switches and sampler values.")

    async def add_lorebook_entry(args: _LoreEntryArgs) -> ToolOutput:
        world = await get_world(args.world_id)
        if world is None:
            raise ToolFailure(f"No world has the id {args.world_id}. list_worlds shows the ids.")
        # The entry route's own schema and writer: an authored entry with its defaults, and a world revision bump.
        fields = LorebookEntryCreate(name=args.name, content=args.content, keywords=list(args.keywords), constant=args.constant)
        entry = await create_lorebook_entry(world["id"], fields.model_dump())
        # This run wrote these texts, so it has seen them whole.
        record.shown("lorebook_entry", entry["id"], "name", entry["name"] or "", args.name)
        record.shown("lorebook_entry", entry["id"], "content", entry["content"] or "", args.content)
        world_name = clip(world["name"], _QUERY_CHARS)
        data = {
            "created": True,
            "world": {"id": world["id"], "name": world_name},
            "entry": {
                "id": entry["id"],
                "name": clip(entry["name"], _QUERY_CHARS),
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

    table = _switch_table()
    toggle_names = ("editor", *table)
    _ToggleArgs = create_model(
        "_ToggleArgs",
        __base__=ToolArguments,
        toggle=(Literal[toggle_names], Field(description="The switch to set.")),
        enabled=(bool, Field(description="true to turn it on, false to turn it off.")),
    )

    def toggle_keys(args: Any) -> list[str]:
        return list(dict.fromkeys(table[name].key for name in _toggle_switches(args.toggle, args.enabled)))

    async def set_pipeline_toggle(args: Any) -> ToolOutput:
        names = _toggle_switches(args.toggle, args.enabled)
        before = await get_settings()
        changed = [name for name in names if table[name].read(before) != args.enabled]
        for name in changed:
            await table[name].write(args.enabled)
        after = await get_settings()
        # The Editor reports both of its switches whichever way it was set, so turning it on cannot hide the Length Guard.
        reported = _EDITOR_OFF if args.toggle == "editor" else names
        state = {name: {"stored": table[name].read(after), "effective": _effective(name, table, after)} for name in reported}
        effective = any(state[name]["effective"] for name in names)
        gates = {
            gate: {"stored": table[gate].read(after), "effective": _effective(gate, table, after)}
            for gate in dict.fromkeys(table[name].gate for name in names)
            if gate is not None
        }
        blocked = [gate for gate, value in gates.items() if not value["effective"]]
        label = "Editor" if args.toggle == "editor" else table[args.toggle].label
        wanted = "on" if args.enabled else "off"
        if not changed:
            summary = f"{label} was already {wanted}."
        else:
            summary = f"Turned {label} {wanted}."
        if args.toggle == "editor" and args.enabled:
            # On sets the Output Auditor alone; the Length Guard is a limit the user sets separately.
            guard = "on" if state["length_guard"]["stored"] else "off; it is set separately"
            summary += f" The Output Auditor is on; the Length Guard stays {guard}."
        if args.enabled and not effective and blocked:
            summary += f" It has no effect while {', '.join(table[gate].label for gate in blocked)} is off."
        data: dict[str, Any] = {
            "toggle": args.toggle,
            "requested": args.enabled,
            "effective": effective,
            "changed": changed,
            "switches": state,
            "gates": gates,
        }
        if args.toggle == "editor":
            data["note"] = "Post-processing and feedback fragments can still run an Editor pass; they are set per fragment."
        if blocked and args.enabled:
            data["note_gates"] = "A master switch is off. Tell the user; turn it on only if they ask."
        keys = list(dict.fromkeys(table[name].key for name in changed))
        return ToolOutput(
            data,
            summary,
            {"toggle": args.toggle, "keys": keys},
            committed=({"kind": "settings", "keys": keys},) if keys else (),
        )

    return [
        _read_tool(
            "get_context",
            "What the user is looking at: the chat's title, kept branch, cast, worlds in effect and context size, "
            "or the open document or character.",
            NoArguments,
            get_context,
        ),
        _read_tool("list_characters", "Search character cards by name, creator or tag.", _ListArgs, list_characters),
        _read_tool(
            "get_character",
            "Read one character's authored text. A group chat has several characters, so name one.",
            _CharacterArgs,
            get_character,
        ),
        _read_tool("query_library", f"Search the library with SQL. {_VIEW_NOTES}", _SqlArgs, query_library),
        _read_tool(
            "read_messages",
            f"Read the latest messages on a chat's kept branch, at most {MAX_MESSAGES}, or one message whole by message_id.",
            _MessagesArgs,
            read_messages,
        ),
        _read_tool("list_worlds", "List worlds (lorebooks) by name.", _ListArgs, list_worlds),
        _read_tool(
            "list_lorebook_entries",
            "List one world's entries with their text, or read one entry's content whole; query matches names, keywords "
            "and content.",
            _EntriesArgs,
            list_lorebook_entries,
        ),
        _read_tool("list_personas", "List the user's personas, or read one's description whole.", _PersonaArgs, list_personas),
        _read_tool(
            "read_document",
            "Without document_id, list documents; with one, read its saved text. Unsaved editor text is never included.",
            _DocumentArgs,
            read_document,
        ),
        _read_tool(
            "get_settings_summary",
            "Pipeline switches and sampler values. No endpoints, models or credentials.",
            NoArguments,
            get_settings_summary,
        ),
        AssistantTool(
            "create_lorebook_entry",
            "Add a new entry to a world (lorebook). It always adds one, even when an entry already has that name; "
            "it never edits an entry.",
            _LoreEntryArgs,
            "write",
            add_lorebook_entry,
            target=lambda args: {"kind": "lorebook_entry", "world_id": args.world_id},
        ),
        AssistantTool(
            "set_pipeline_toggle",
            _toggle_description(table),
            _ToggleArgs,
            "write",
            set_pipeline_toggle,
            target=lambda args: {"kind": "settings", "keys": toggle_keys(args)},
            idempotent=True,
        ),
        *build_edit_tools(view, origin or Origin("", _wire(view)), record),
        *build_action_tools(view, record),
        *build_media_tools(view, media),
    ]
