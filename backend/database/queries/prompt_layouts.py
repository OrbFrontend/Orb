"""Prompt layouts and their ordered blocks."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any, cast

import aiosqlite

from ...core.prompt_layout import (
    CUSTOM_KIND,
    DEFAULT_LAYOUT_ID,
    EDITABLE_MARKERS,
    MARKER_TEMPLATES,
    LayoutBlock,
    LayoutRegion,
    LayoutRole,
    default_layout,
    default_role,
    marker_block_id,
    marker_placeholder,
    names_marker_placeholder,
)
from ..connection import get_db, select_rows
from ..models import PromptBlockRow, PromptLayoutRow
from ..seeds import SEED_MAIN_PROMPT

_BLOCK_COLUMNS = "id, layout_id, kind, label, content, role, region, sort_order, enabled"


class PromptLayoutError(ValueError):
    """A layout edit the rules refuse; the message is safe to show."""


async def insert_layout_blocks(db: aiosqlite.Connection, layout_id: str, blocks: Sequence[LayoutBlock]) -> None:
    await db.executemany(
        f"INSERT INTO prompt_blocks ({_BLOCK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",  # nosec B608 -- fixed columns
        [(b.id, layout_id, b.kind, b.label, b.content, b.role, b.region, b.sort_order, int(b.enabled)) for b in blocks],
    )


async def list_prompt_layouts() -> list[PromptLayoutRow]:
    rows = await select_rows("SELECT id, name FROM prompt_layouts ORDER BY id != ?, name COLLATE NOCASE", (DEFAULT_LAYOUT_ID,))
    return [cast(PromptLayoutRow, dict(row)) for row in rows]


async def get_prompt_layout(layout_id: str) -> PromptLayoutRow | None:
    rows = await select_rows("SELECT id, name FROM prompt_layouts WHERE id = ?", (layout_id,))
    return cast(PromptLayoutRow, dict(rows[0])) if rows else None


async def get_active_prompt_layout_id() -> str:
    rows = await select_rows(
        "SELECT l.id FROM settings s JOIN prompt_layouts l ON l.id = s.active_prompt_layout_id WHERE s.id = 1"
    )
    return str(rows[0]["id"]) if rows else DEFAULT_LAYOUT_ID


async def get_prompt_blocks(layout_id: str) -> list[PromptBlockRow]:
    rows = await select_rows(
        f"SELECT {_BLOCK_COLUMNS} FROM prompt_blocks WHERE layout_id = ? ORDER BY sort_order, rowid",  # nosec B608
        (layout_id,),
    )
    return [cast(PromptBlockRow, dict(row)) for row in rows]


async def get_prompt_block(block_id: str) -> PromptBlockRow | None:
    rows = await select_rows(f"SELECT {_BLOCK_COLUMNS} FROM prompt_blocks WHERE id = ?", (block_id,))  # nosec B608
    return cast(PromptBlockRow, dict(rows[0])) if rows else None


def layout_block(row: PromptBlockRow) -> LayoutBlock:
    return LayoutBlock(
        id=row["id"],
        kind=row["kind"],
        label=row["label"],
        content=row["content"],
        role=row["role"],
        region=row["region"],
        sort_order=row["sort_order"],
        enabled=bool(row["enabled"]),
    )


async def get_active_layout_blocks() -> tuple[LayoutBlock, ...]:
    """The active layout's blocks in order; the built-in order when no layout rows exist."""
    rows = await get_prompt_blocks(await get_active_prompt_layout_id())
    return tuple(layout_block(row) for row in rows) if rows else default_layout(main_prompt=SEED_MAIN_PROMPT)


async def create_prompt_layout(name: str, copy_from: str | None = None) -> PromptLayoutRow | None:
    """A new layout seeded with the built-in order, or a copy of *copy_from*'s blocks; None when *copy_from* is missing."""
    name = name.strip()
    if not name:
        raise PromptLayoutError("A layout needs a name.")
    layout_id = uuid.uuid4().hex
    if copy_from is not None:
        source = await get_prompt_blocks(copy_from)
        if not source:
            return None
        blocks = [
            replace(
                layout_block(row),
                id=uuid.uuid4().hex if row["kind"] == CUSTOM_KIND else marker_block_id(layout_id, row["kind"]),
            )
            for row in source
        ]
    else:
        blocks = list(default_layout(layout_id, SEED_MAIN_PROMPT))
    async with get_db() as db:
        await db.execute("INSERT INTO prompt_layouts (id, name) VALUES (?, ?)", (layout_id, name))
        await insert_layout_blocks(db, layout_id, blocks)
        await db.commit()
    return {"id": layout_id, "name": name}


async def rename_prompt_layout(layout_id: str, name: str) -> PromptLayoutRow | None:
    name = name.strip()
    if not name:
        raise PromptLayoutError("A layout needs a name.")
    async with get_db() as db:
        await db.execute("UPDATE prompt_layouts SET name = ? WHERE id = ?", (name, layout_id))
        await db.commit()
    return await get_prompt_layout(layout_id)


async def delete_prompt_layout(layout_id: str) -> bool:
    """Deletes the layout; deleting the active one activates the first layout left."""
    if await get_prompt_layout(layout_id) is None:
        return False
    remaining = [row["id"] for row in await list_prompt_layouts() if row["id"] != layout_id]
    if not remaining:
        raise PromptLayoutError("The last layout cannot be deleted.")
    was_active = layout_id == await get_active_prompt_layout_id()
    async with get_db() as db:
        await db.execute("DELETE FROM prompt_layouts WHERE id = ?", (layout_id,))
        if was_active:
            await db.execute("UPDATE settings SET active_prompt_layout_id = ? WHERE id = 1", (remaining[0],))
        await db.commit()
    return True


async def set_active_prompt_layout(layout_id: str) -> bool:
    if await get_prompt_layout(layout_id) is None:
        return False
    async with get_db() as db:
        await db.execute("UPDATE settings SET active_prompt_layout_id = ? WHERE id = 1", (layout_id,))
        await db.commit()
    return True


async def create_prompt_block(layout_id: str, data: Mapping[str, Any]) -> PromptBlockRow | None:
    """A custom block at the end of the layout. None when the layout is missing."""
    if await get_prompt_layout(layout_id) is None:
        return None
    block_id = uuid.uuid4().hex
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT COALESCE(MAX(sort_order), -1) AS top FROM prompt_blocks WHERE layout_id = ?", (layout_id,)
            )
        )
        block = LayoutBlock(
            id=block_id,
            kind=CUSTOM_KIND,
            label=data["label"].strip(),
            content=data["content"],
            role=data["role"] or default_role(data["region"]),
            region=data["region"],
            sort_order=int(rows[0]["top"]) + 1,
        )
        await insert_layout_blocks(db, layout_id, [block])
        await db.commit()
    return await get_prompt_block(block_id)


async def update_prompt_block(block_id: str, data: Mapping[str, Any]) -> PromptBlockRow | None:
    """Edit a block's label, text, role or switch. A marker keeps its name, only an editable marker's text changes, keeping its
    placeholder. :func:`reorder_prompt_blocks` moves blocks."""
    row = await get_prompt_block(block_id)
    if row is None:
        return None
    kind = row["kind"]
    custom = kind == CUSTOM_KIND
    if "content" in data:
        if not custom and kind not in EDITABLE_MARKERS:
            raise PromptLayoutError("A built-in block cannot be edited.")
        if kind in MARKER_TEMPLATES and not names_marker_placeholder(kind, data["content"]):
            raise PromptLayoutError(f"Keep {marker_placeholder(kind)} in this block's text.")
    if not custom and "label" in data:
        raise PromptLayoutError("A built-in block keeps its name.")
    updates = {column: data[column] for column in ("label", "content", "role", "enabled") if column in data}
    if "label" in updates:
        updates["label"] = updates["label"].strip()
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    if updates:
        sets = ", ".join(f"{column} = ?" for column in updates)
        async with get_db() as db:
            await db.execute(
                f"UPDATE prompt_blocks SET {sets} WHERE id = ?",  # nosec B608 -- columns from the fixed set above
                [*updates.values(), block_id],
            )
            await db.commit()
    return await get_prompt_block(block_id)


async def delete_prompt_block(block_id: str) -> bool:
    row = await get_prompt_block(block_id)
    if row is None:
        return False
    if row["kind"] != CUSTOM_KIND:
        raise PromptLayoutError("A built-in block cannot be deleted.")
    async with get_db() as db:
        await db.execute("DELETE FROM prompt_blocks WHERE id = ?", (block_id,))
        await db.commit()
    return True


def _crossed_role(row: PromptBlockRow, region: LayoutRegion) -> LayoutRole:
    """*row*'s role once placed in *region*."""
    if row["region"] != region and row["role"] == default_role(row["region"]):
        return default_role(region)
    return row["role"]


async def reorder_prompt_blocks(layout_id: str, prefix: Sequence[str], writer: Sequence[str]) -> list[PromptBlockRow] | None:
    """Place every block of the layout: *prefix* ids above the chat history, *writer* ids below, each in order. A block that
    crosses the history with its region's default role takes the new region's default; any other role stays. None when the
    layout is missing."""
    rows = {row["id"]: row for row in await get_prompt_blocks(layout_id)}
    if not rows:
        return None
    placed: list[tuple[LayoutRegion, str]] = [
        *(("prefix", block_id) for block_id in prefix),
        *(("writer", block_id) for block_id in writer),
    ]
    ids = [block_id for _, block_id in placed]
    if len(ids) != len(set(ids)) or set(ids) != set(rows):
        raise PromptLayoutError("The order must list every block of the layout exactly once.")
    if any(rows[block_id]["kind"] != CUSTOM_KIND and rows[block_id]["region"] != region for region, block_id in placed):
        raise PromptLayoutError("This block cannot cross the chat history.")
    async with get_db() as db:
        await db.executemany(
            "UPDATE prompt_blocks SET sort_order = ?, region = ?, role = ? WHERE id = ?",
            [
                (index, region, _crossed_role(rows[block_id], region), block_id)
                for index, (region, block_id) in enumerate(placed)
            ],
        )
        await db.commit()
    return await get_prompt_blocks(layout_id)
