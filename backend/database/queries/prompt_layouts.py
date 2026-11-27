"""A profile's prompt layout: its ordered blocks. ``queries.profiles`` owns the profiles themselves."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

import aiosqlite

from ...core.prompt_layout import (
    CUSTOM_KIND,
    EDITABLE_MARKERS,
    MARKER_TEMPLATES,
    LayoutBlock,
    LayoutRegion,
    LayoutRole,
    default_role,
    marker_placeholder,
    names_marker_placeholder,
)
from ..connection import get_db, select_rows
from ..models import PromptBlockRow

_BLOCK_COLUMNS = "id, layout_id, kind, label, content, role, region, sort_order, enabled"


class PromptLayoutError(ValueError):
    """A layout edit the rules refuse; the message is safe to show."""


async def insert_layout_blocks(db: aiosqlite.Connection, layout_id: str, blocks: Sequence[LayoutBlock]) -> None:
    await db.executemany(
        f"INSERT INTO prompt_blocks ({_BLOCK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",  # nosec B608 -- fixed columns
        [(b.id, layout_id, b.kind, b.label, b.content, b.role, b.region, b.sort_order, int(b.enabled)) for b in blocks],
    )


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


async def create_prompt_block(layout_id: str, data: Mapping[str, Any]) -> PromptBlockRow | None:
    """A custom block at the end of the profile's layout. None when the profile is missing."""
    if not await select_rows("SELECT 1 FROM profiles WHERE id = ?", (layout_id,)):
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
