"""The assistant's setters: a lore entry's switches and keywords, a world's name and flags, the Length Guard's bounds, and the
phrase bank.

Each goes through its resource's own schema and writer and writes only what differs from what is stored. A setter names the
state wanted, never a flip, so all but the phrase bank may run again and change nothing.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from pydantic import Field, StringConstraints, field_validator

from ...database import (
    add_phrase_group,
    get_phrase_bank_rows,
    get_settings,
    update_lorebook_entry,
    update_settings,
    update_world,
)
from ...features.assistant import AssistantTool, ToolArguments, ToolFailure, ToolOutput, ToolSet
from ..deps import validate_phrase_group
from ..schemas import LorebookEntryUpdate, WorldUpdate
from .targets import ID_CHARS, clip, entry_of, not_blank, quoted, world_of
from .toggles import switch_summary

_NAME_CHARS = 200
_KEYWORD_CHARS = 100
_KEYWORDS = 20
_PHRASE_CHARS = 200
_VARIANTS = 10
# The bounds the Length Guard's own fields keep.
WORDS_RANGE = (50, 4000)
PARAGRAPHS_RANGE = (1, 20)

# A flag a setter may leave, turn on or turn off.
Switch = Literal["unchanged", "on", "off"]
_SWITCH = "on or off; unchanged leaves it as it is."

_Keyword = Annotated[str, StringConstraints(min_length=1, max_length=_KEYWORD_CHARS)]
_Phrase = Annotated[str, StringConstraints(min_length=1, max_length=_PHRASE_CHARS)]


def _flag(key: str, value: Switch) -> dict[str, bool]:
    """``{key: on}`` for a flag the caller set, and nothing for one left unchanged."""
    return {} if value == "unchanged" else {key: value == "on"}


class _EntrySet(ToolArguments):
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The entry's world, from list_worlds.")
    entry_id: int = Field(ge=1, description="The entry's id, from list_lorebook_entries.")
    enabled: Switch = Field(default="unchanged", description=f"Whether the entry is used at all: {_SWITCH}")
    constant: Switch = Field(
        default="unchanged", description=f"Whether it is used in every chat with its world, keywords or not: {_SWITCH}"
    )
    keywords: list[_Keyword] = Field(
        default_factory=list,
        max_length=_KEYWORDS,
        description="The entry's complete new keyword list, replacing the old one; empty leaves the keywords as they are.",
    )

    _blank = field_validator("keywords")(not_blank)


class _WorldSet(ToolArguments):
    world_id: str = Field(min_length=1, max_length=ID_CHARS, description="The world (lorebook), from list_worlds.")
    name: str = Field(default="", max_length=_NAME_CHARS, description="A new name; empty leaves the name as it is.")
    is_global: Switch = Field(
        default="unchanged", description=f"Whether every chat uses the world unless it turns it off: {_SWITCH}"
    )
    dynamic_enabled: Switch = Field(
        default="unchanged",
        description=f"Dynamic Worlds, where chats propose lore changes for review: {_SWITCH}",
    )


class _LengthGuardSet(ToolArguments):
    max_words: int = Field(
        default=0, ge=0, description=f"Most words in a reply, {WORDS_RANGE[0]} to {WORDS_RANGE[1]}; 0 leaves it as it is."
    )
    max_paragraphs: int = Field(
        default=0,
        ge=0,
        description=f"Most paragraphs in a reply, {PARAGRAPHS_RANGE[0]} to {PARAGRAPHS_RANGE[1]}; 0 leaves it as it is.",
    )

    @field_validator("max_words")
    @classmethod
    def _words(cls, value: int) -> int:
        return _bounded(value, WORDS_RANGE)

    @field_validator("max_paragraphs")
    @classmethod
    def _paragraphs(cls, value: int) -> int:
        return _bounded(value, PARAGRAPHS_RANGE)


def _bounded(value: int, bounds: tuple[int, int]) -> int:
    if value and not bounds[0] <= value <= bounds[1]:
        raise ValueError(f"must be 0 or from {bounds[0]} to {bounds[1]}")
    return value


class _PhraseAdd(ToolArguments):
    phrase: _Phrase = Field(description="The phrase replies should not use, as written.")
    variants: list[_Phrase] = Field(
        default_factory=list,
        max_length=_VARIANTS,
        description="Other wordings of the same phrase, caught as the same group.",
    )

    _blank = field_validator("phrase", "variants")(not_blank)


def build_setter_tools() -> list[AssistantTool]:
    """The setters, in their stable order."""
    tools = ToolSet()

    @tools.add(
        "write",
        _EntrySet,
        "Turn a lore entry on or off, make it constant or keyword-triggered, or replace its keywords. "
        "Turning an entry off is how to undo one added by mistake.",
        idempotent=True,
    )
    async def set_lorebook_entry(args: _EntrySet) -> ToolOutput:
        world, entry = await entry_of(args.world_id, args.entry_id)
        wanted: dict[str, Any] = {
            **_flag("enabled", args.enabled),
            **_flag("constant", args.constant),
            **({"keywords": list(dict.fromkeys(args.keywords))} if args.keywords else {}),
        }
        if not wanted:
            raise ToolFailure("Name enabled, constant or keywords; nothing was asked to change.")
        stored = {"enabled": bool(entry["enabled"]), "constant": bool(entry["constant"]), "keywords": entry["keywords"] or []}
        changes = {key: value for key, value in wanted.items() if stored[key] != value}
        if changes:
            # The entry route's own schema and writer: the entry and world revisions advance as for any edit.
            fields = LorebookEntryUpdate(**changes).model_dump(exclude_unset=True)
            if await update_lorebook_entry(entry["id"], fields, world_id=world["id"]) is None:
                raise ToolFailure(f"That world has no entry {args.entry_id}.")
        after = {**stored, **changes}
        data = {
            "world": {"id": world["id"], "name": clip(world["name"], _NAME_CHARS)},
            "entry": {"id": entry["id"], "name": clip(entry["name"], _NAME_CHARS), **after},
            "changed": sorted(changes),
        }
        if after["constant"] is False and not after["keywords"]:
            data["note"] = "It is keyword-triggered with no keywords, so nothing brings it into a chat."
        label = f"lore entry {quoted(entry['name'])}"
        ids = {"world_id": world["id"], "entry_id": entry["id"]}
        if not changes:
            return ToolOutput(data, f"The {label} already had that; nothing changed.", ids)
        said = [
            *([("on" if changes["enabled"] else "off")] if "enabled" in changes else []),
            *([("constant" if changes["constant"] else "keyword-triggered")] if "constant" in changes else []),
            *([f"keywords {json.dumps(changes['keywords'], ensure_ascii=False)}"] if "keywords" in changes else []),
        ]
        return ToolOutput(data, f"Set the {label}: {'; '.join(said)}.", ids, committed=({"kind": "lorebook_entry", **ids},))

    @tools.add(
        "write",
        _WorldSet,
        "Rename a world (lorebook), make it global or not, or turn Dynamic Worlds on or off for it.",
        idempotent=True,
    )
    async def set_world(args: _WorldSet) -> ToolOutput:
        world = await world_of(args.world_id)
        wanted: dict[str, Any] = {
            **({"name": args.name.strip()} if args.name.strip() else {}),
            **_flag("is_global", args.is_global),
            **_flag("dynamic_enabled", args.dynamic_enabled),
        }
        if not wanted:
            raise ToolFailure("Name a name, is_global or dynamic_enabled; nothing was asked to change.")
        stored = {
            "name": world["name"],
            "is_global": bool(world["is_global"]),
            "dynamic_enabled": bool(world["dynamic_enabled"]),
        }
        changes = {key: value for key, value in wanted.items() if stored[key] != value}
        if changes:
            # The world route's own schema and writer; a world's own fields never invalidate its pending proposals.
            if await update_world(world["id"], WorldUpdate(**changes).model_dump(exclude_unset=True)) is None:
                raise ToolFailure(f"No world has the id {args.world_id}.")
        after = {**stored, **changes}
        data: dict[str, Any] = {
            "world": {"id": world["id"], **{**after, "name": clip(after["name"], _NAME_CHARS)}},
            "changed": sorted(changes),
        }
        if changes.get("is_global"):
            data["note"] = "Every chat now uses this world unless that chat turns it off."
        ids = {"world_id": world["id"]}
        if not changes:
            return ToolOutput(data, f"World {quoted(world['name'])} already had that; nothing changed.", ids)
        said = [
            *([f"renamed to {quoted(changes['name'])}"] if "name" in changes else []),
            *([("global" if changes["is_global"] else "not global")] if "is_global" in changes else []),
            *([f"Dynamic Worlds {'on' if changes['dynamic_enabled'] else 'off'}"] if "dynamic_enabled" in changes else []),
        ]
        return ToolOutput(
            data, f"Set world {quoted(world['name'])}: {'; '.join(said)}.", ids, committed=({"kind": "world", **ids},)
        )

    @tools.add(
        "write",
        _LengthGuardSet,
        "Set the Length Guard's limits on a reply's words or paragraphs. It does not turn the Length Guard on or off; "
        "set_pipeline_toggle does.",
        idempotent=True,
    )
    async def set_length_guard(args: _LengthGuardSet) -> ToolOutput:
        wanted = {
            **({"length_guard_max_words": args.max_words} if args.max_words else {}),
            **({"length_guard_max_paragraphs": args.max_paragraphs} if args.max_paragraphs else {}),
        }
        if not wanted:
            raise ToolFailure("Name max_words or max_paragraphs; nothing was asked to change.")
        before = await get_settings()
        changes = {key: value for key, value in wanted.items() if before.get(key) != value}
        if changes:
            # The settings route's own writer: these columns only, so a concurrent change to any other key is untouched.
            await update_settings(changes)
        switches = switch_summary(await get_settings())
        guard = switches["length_guard_limits"]
        data = {"length_guard": switches["length_guard"], "limits": guard, "changed": sorted(changes)}
        if switches["length_guard"] is not True:
            data["note"] = "The Length Guard is not in effect, so these limits apply only once it is on."
        limits = f"{guard['max_words']} words, {guard['max_paragraphs']} paragraphs"
        if not changes:
            return ToolOutput(data, f"The Length Guard already allows {limits}; nothing changed.")
        return ToolOutput(
            data,
            f"Set the Length Guard to at most {limits}.",
            {"keys": sorted(changes)},
            committed=({"kind": "settings", "keys": sorted(changes)},),
        )

    @tools.add(
        "write",
        _PhraseAdd,
        "Add a phrase to the phrase bank, which the Output Auditor removes from replies; variants are other wordings of it, "
        "caught as one group. A phrase already in the bank is not added again.",
    )
    async def add_banned_phrase(args: _PhraseAdd) -> ToolOutput:
        try:
            variants, _ = validate_phrase_group("literal", [args.phrase, *args.variants], "")
        except HTTPException as exc:
            raise ToolFailure(str(exc.detail)) from None
        variants = list(dict.fromkeys(variants))
        known = {
            str(variant).casefold(): row["id"] for row in await get_phrase_bank_rows() for variant in row["variants"] or []
        }
        already = [variant for variant in variants if variant.casefold() in known]
        if already:
            data = {"added": False, "already_in_bank": already, "group_id": known[already[0].casefold()]}
            return ToolOutput(data, f"The phrase bank already has {json.dumps(already[0], ensure_ascii=False)}; nothing added.")
        group_id = await add_phrase_group(variants)
        switches = switch_summary(await get_settings())
        auditor = switches["output_auditor"]
        check = switches["editor_audit_checks"].get("banned_phrases")
        data = {"added": True, "group_id": group_id, "variants": variants}
        if auditor is not True or check is False:
            data["note"] = (
                "The phrase bank is applied by the Output Auditor's banned-phrases check, which is not in effect now. "
                "Tell the user; turn it on only if they ask."
            )
        return ToolOutput(
            data,
            f"Added {json.dumps(variants[0], ensure_ascii=False)} to the phrase bank"
            + (f" with {len(variants) - 1} variants." if len(variants) > 1 else "."),
            {"group_id": group_id},
            committed=({"kind": "phrase_bank", "group_id": group_id},),
        )

    return tools
