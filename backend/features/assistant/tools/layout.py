"""The assistant's prompt layout tools: the active profile's blocks, read, created, switched and moved.

A block's text and a custom block's label change through ``edit_prompt_block`` and a custom block goes through
``delete_prompt_block``, both proposals. Every tool here acts on the profile active when it runs; a proposal names the profile
it was made in, so an Apply after a switch still writes that one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal

from pydantic import Field, field_validator

from ....core.macros import has_clock_macros
from ....core.prompt_layout import (
    CUSTOM_KIND,
    EDITABLE_MARKERS,
    LayoutRegion,
    default_role,
    marker_placeholder,
    marker_text_problem,
    required_placeholder,
)
from ....database import (
    PromptLayoutError,
    create_prompt_block,
    get_active_profile_id,
    get_profile,
    get_prompt_block,
    get_prompt_blocks,
    reorder_prompt_blocks,
    update_prompt_block,
)
from ....database.models import PromptBlockRow
from ..contracts import (
    MAX_ITEM_CHARS,
    AssistantTool,
    NoArguments,
    ToolArguments,
    ToolFailure,
    ToolOutput,
    ToolSet,
    encode_result,
)
from .reads import text_page
from .setters import Switch
from .targets import ID_CHARS, clip, not_blank, quoted

# The longest a block's label and text may be, as the layout window holds them.
BLOCK_TEXT_CAPS = {"label": 200, "content": 50_000}
_PREVIEWS = (300, 160, 80, 0)
_RESULT_BUDGET = 5900
_REGIONS = "prefix: above Chat history, part of every pass's prompt; writer: below it, the Writer's request only."
_CLOCK = (
    "{{time}} or {{date}} above Chat history changes the prompt's start whenever the clock moves, so the model re-reads "
    "the whole chat each time."
)


def block_field() -> Any:
    return Field(min_length=1, max_length=ID_CHARS, description="The block, from list_prompt_blocks.")


async def profile_name(profile_id: str) -> str:
    profile = await get_profile(profile_id)
    return profile["name"] if profile else ""


async def block_of(block_id: str) -> PromptBlockRow:
    """Block *block_id* of the active profile's layout, or a failure that says where the ids are."""
    row = await get_prompt_block(block_id)
    if row is None:
        raise ToolFailure(f"The active profile's layout has no block with the id {block_id}. list_prompt_blocks shows the ids.")
    return row


def block_label(row: Mapping[str, Any], profile: str) -> str:
    return f"prompt block {quoted(row['label'] or 'Untitled block')} in profile {quoted(profile)}"


def is_marker(row: Mapping[str, Any]) -> bool:
    return row["kind"] != CUSTOM_KIND


def text_editable(row: Mapping[str, Any]) -> bool:
    return not is_marker(row) or row["kind"] in EDITABLE_MARKERS


def check_block_field(row: Mapping[str, Any], name: str) -> None:
    """Refuse an edit to a field *row* keeps fixed: a built-in block's name, and the text of a marker that has none."""
    if name == "label" and is_marker(row):
        raise ToolFailure("A built-in block keeps its name. Nothing was proposed.")
    if name == "content" and not text_editable(row):
        raise ToolFailure(
            f"The {row['label']} block's text is the engine's own and cannot be edited; set_prompt_block can turn it off or "
            "move it. Nothing was proposed."
        )


def block_text_problem(row: Mapping[str, Any], name: str, text: str) -> str:
    """Why *text* cannot become field *name* of *row*, as the layout's own rules refuse it; or nothing."""
    cap = BLOCK_TEXT_CAPS[name]
    if len(text) > cap:
        return f"That is {len(text):,} characters; a block's {name} may be at most {cap:,}."
    if name == "label" and text != text.strip():
        return "A label has no space at either end."
    if name == "content" and is_marker(row):
        return marker_text_problem(row["kind"], row["label"], text)
    return ""


def _ordered(rows: Sequence[PromptBlockRow], region: str) -> list[PromptBlockRow]:
    return [row for row in rows if row["region"] == region]


def _row(row: PromptBlockRow, position: int, cap: int) -> dict[str, Any]:
    text = row["content"] if text_editable(row) else ""
    return {
        "id": row["id"],
        "position": position,
        "label": row["label"],
        "kind": row["kind"],
        "role": row["role"],
        "enabled": bool(row["enabled"]),
        "editable": text_editable(row),
        **({"chars": len(text)} if text else {}),
        **({"text": clip(text, cap)} if text and cap else {}),
        **({"clock": True} if row["region"] == "prefix" and has_clock_macros(text) else {}),
    }


def _fit(build: Callable[[int], dict[str, Any]]) -> dict[str, Any]:
    for cap in _PREVIEWS:
        data = build(cap)
        if len(encode_result(data)) <= _RESULT_BUDGET:
            return data
    raise ToolFailure("The layout has too many blocks to list in one result.")


class _BlockRead(ToolArguments):
    block_id: str = block_field()
    text_offset: int = Field(default=0, ge=0, description="To read on in a long text: the last page's next_offset.")


class _BlockCreate(ToolArguments):
    label: str = Field(min_length=1, max_length=BLOCK_TEXT_CAPS["label"], description="The name the layout shows.")
    content: str = Field(
        min_length=1,
        max_length=MAX_ITEM_CHARS,
        description="Its text. Macros such as {{char}} and section placeholders such as {{scenario}} work here.",
    )
    region: LayoutRegion = Field(description=_REGIONS)
    role: Literal["default", "system", "user", "assistant"] = Field(
        default="default", description="The message role it is sent as; default is system above Chat history, user below."
    )
    position: int = Field(default=0, ge=0, description="Its place among its region's blocks, from 1; 0 puts it last.")
    enabled: bool = True

    _blank = field_validator("label", "content")(not_blank)


class _BlockSet(ToolArguments):
    block_id: str = block_field()
    enabled: Switch = Field(
        default="unchanged", description="Whether the block is in the prompt: on or off; unchanged leaves it."
    )
    role: Literal["unchanged", "default", "system", "user", "assistant"] = Field(
        default="unchanged",
        description="The message role it is sent as; default is system above Chat history, user below.",
    )
    region: Literal["unchanged", "prefix", "writer"] = Field(
        default="unchanged",
        description=f"Move a custom block across Chat history ({_REGIONS}) Built-in blocks stay on their side.",
    )
    position: int = Field(
        default=0,
        ge=0,
        description="Its place among its region's blocks, from 1, counted with it in place; 0 leaves it, or with region puts "
        "it last.",
    )


async def _place(rows: Sequence[PromptBlockRow], block_id: str, region: str, position: int, profile_id: str) -> None:
    """Move block *block_id* to *position* (from 1, 0 last) among *region*'s blocks, keeping every other block's order."""
    order = {side: [row["id"] for row in _ordered(rows, side) if row["id"] != block_id] for side in ("prefix", "writer")}
    side = order[region]
    side.insert(len(side) if not position else min(position, len(side) + 1) - 1, block_id)
    try:
        await reorder_prompt_blocks(order["prefix"], order["writer"], profile_id)
    except PromptLayoutError as exc:
        raise ToolFailure(f"{exc} Nothing was moved.") from None


def _position(rows: Sequence[PromptBlockRow], block_id: str) -> tuple[LayoutRegion, int]:
    row = next(row for row in rows if row["id"] == block_id)
    return row["region"], [item["id"] for item in _ordered(rows, row["region"])].index(block_id) + 1


def build_layout_tools() -> list[AssistantTool]:
    """The prompt layout's reads, its create and its setter, over the active profile."""
    tools = ToolSet()

    @tools.add(
        "read",
        NoArguments,
        "List the active profile's prompt layout: the blocks the chat prompt is built from, in order, on each side of the "
        "fixed Chat history divider. Blocks above it (prefix) build the system prompt every pass reads; blocks below it "
        "(writer) build the Writer's request after the chat. A block with a kind other than custom is built-in: it stands for "
        "an engine part (main_prompt, cast, the lore markers, scene_guidance, user_message, speaker) and keeps "
        "its name and side. The cast block's text is each group member's card, written once per member the group's context "
        "mode shows; in it {{char}}, {{description}}, {{personality}} and {{examples}} stand for that member. The speaker "
        "block heads the Writer request on a group turn only; in it {{char}} is the speaker, "
        "{{instructions}} the speaker card's post-history instructions and {{card}} the speaker's card under Private. Each "
        "block is sent as a message of its role.",
    )
    async def list_prompt_blocks(_args: NoArguments) -> ToolOutput:
        profile_id = await get_active_profile_id()
        rows = await get_prompt_blocks(profile_id)
        name = await profile_name(profile_id)

        def build(cap: int) -> dict[str, Any]:
            sides = {
                side: [_row(row, index, cap) for index, row in enumerate(_ordered(rows, side), 1)]
                for side in ("prefix", "writer")
            }
            clipped = any(row.get("text", "").endswith(" chars]") for side in sides.values() for row in side)
            note = {"note": "Text ending with …[+N chars] was shortened; read_prompt_block reads it whole."} if clipped else {}
            return {"profile": name, **sides, **note}

        data = _fit(build)
        return ToolOutput(
            data, f"Listed the {len(rows)} blocks of profile {quoted(name)}'s layout.", {"profile_id": profile_id}
        )

    @tools.add("read", _BlockRead, "Read one prompt block whole, its text one page per text_offset.")
    async def read_prompt_block(args: _BlockRead) -> ToolOutput:
        row = await block_of(args.block_id)
        region, position = _position(await get_prompt_blocks(), row["id"])
        head = {
            "label": row["label"],
            "kind": row["kind"],
            "region": region,
            "position": position,
            "role": row["role"],
            "enabled": bool(row["enabled"]),
            "editable": text_editable(row),
            **({"placeholder": marker_placeholder(name)} if (name := required_placeholder(row["kind"])) else {}),
        }
        if not text_editable(row):
            return ToolOutput(head, f"Read the {row['label']} block.", {"block_id": row["id"]})
        data = text_page(row["content"], args.text_offset, head, "text")
        return ToolOutput(data, f"Read the {row['label'] or 'untitled'} block.", {"block_id": row["id"]})

    @tools.add("write", _BlockCreate, "Add a custom block to the active profile's prompt layout.", name="create_prompt_block")
    async def add_prompt_block(args: _BlockCreate) -> ToolOutput:
        profile_id = await get_active_profile_id()
        row = await create_prompt_block(
            {
                "label": args.label,
                "content": args.content,
                "region": args.region,
                "role": None if args.role == "default" else args.role,
            },
            profile_id,
        )
        ids = {"profile_id": profile_id, "block_id": row["id"]}
        committed = ({"kind": "prompt_block", **ids},)
        if not args.enabled:
            await update_prompt_block(row["id"], {"enabled": False}, profile_id)
        rows = await get_prompt_blocks(profile_id)
        if args.position and args.position < len(_ordered(rows, args.region)):
            await _place(rows, row["id"], args.region, args.position, profile_id)
            rows = await get_prompt_blocks(profile_id)
        region, position = _position(rows, row["id"])
        data: dict[str, Any] = {"created": True, "block_id": row["id"], "position": position, "role": row["role"]}
        if region == "prefix" and has_clock_macros(args.content):
            data["note"] = _CLOCK
        summary = f"Added the {quoted(row['label'])} block to profile {quoted(await profile_name(profile_id))}'s layout."
        return ToolOutput(data, summary, ids, committed=committed)

    @tools.add(
        "write",
        _BlockSet,
        "Turn a prompt block on or off, set the role it is sent as, or move it: to another place on its side, or a custom "
        "block across Chat history. A switched-off built-in block only leaves the prompt; what produces it still runs.",
        idempotent=True,
    )
    async def set_prompt_block(args: _BlockSet) -> ToolOutput:
        row = await block_of(args.block_id)
        profile_id = row["profile_id"]
        if args.enabled == "unchanged" and args.role == "unchanged" and args.region == "unchanged" and not args.position:
            raise ToolFailure("Name enabled, role, region or position; nothing was asked to change.")
        if args.region not in ("unchanged", row["region"]) and is_marker(row):
            raise ToolFailure(
                f"The {row['label']} block is built-in and stays on its side of Chat history. Nothing was changed."
            )
        rows = await get_prompt_blocks(profile_id)
        region, position = _position(rows, row["id"])
        changed: list[str] = []
        target: LayoutRegion = region if args.region == "unchanged" else args.region
        if target != region or (args.position and args.position != position):
            await _place(rows, row["id"], target, args.position, profile_id)
            rows = await get_prompt_blocks(profile_id)
            moved = _position(rows, row["id"])
            if moved != (region, position):
                changed.append("region" if moved[0] != region else "position")
            region, position = moved
        current = next(item for item in rows if item["id"] == row["id"])
        wanted: dict[str, Any] = {}
        if args.enabled != "unchanged":
            wanted["enabled"] = args.enabled == "on"
        if args.role != "unchanged":
            wanted["role"] = default_role(region) if args.role == "default" else args.role
        updates = {key: value for key, value in wanted.items() if current[key] != (int(value) if key == "enabled" else value)}
        if updates:
            after = await update_prompt_block(row["id"], updates, profile_id)
            if after is None:
                raise ToolFailure(f"The block {args.block_id} was removed meanwhile.")
            current = after
            changed.extend(sorted(updates))
        if current["role"] != row["role"] and "role" not in changed:
            # Crossing Chat history with its side's default role takes the new side's.
            changed.append("role")
        data = {
            "block": current["label"],
            "region": region,
            "position": position,
            "role": current["role"],
            "enabled": bool(current["enabled"]),
            "changed": changed,
        }
        label, ids = f"{current['label'] or 'untitled'} block", {"profile_id": profile_id, "block_id": row["id"]}
        if not changed:
            return ToolOutput(data, f"The {label} already had that; nothing changed.", ids)
        return ToolOutput(data, f"Set the {label}: {', '.join(changed)}.", ids, committed=({"kind": "prompt_block", **ids},))

    return tools
