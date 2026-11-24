"""Prompt layouts and their ordered blocks."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

import aiosqlite

from ...core.prompt_layout import (
    CUSTOM_KIND,
    DEFAULT_LAYOUT_ID,
    MARKERS,
    LayoutBlock,
    LayoutRegion,
    LayoutRole,
    default_layout,
    marker_block_id,
    marker_region,
)
from ..connection import get_db, select_rows
from ..models import PromptBlockRow, PromptLayoutRow
from ..seeds import SEED_MAIN_PROMPT

_REGIONS = ("prefix", "writer")
_ROLES = ("system", "user", "assistant")
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


def layout_block(row: Mapping[str, Any]) -> LayoutBlock:
    region = row["region"] if row["region"] in _REGIONS else marker_region(row["kind"])
    role = row["role"] if row["role"] in _ROLES else "system"
    return LayoutBlock(
        id=row["id"],
        kind=row["kind"],
        label=row["label"],
        content=row["content"],
        role=cast(LayoutRole, role),
        region=cast(LayoutRegion, region),
        sort_order=int(row["sort_order"]),
        enabled=bool(row["enabled"]),
    )


async def get_active_layout_blocks() -> tuple[LayoutBlock, ...]:
    """The active layout's blocks in order; the built-in order when no layout rows exist."""
    rows = await get_prompt_blocks(await get_active_prompt_layout_id())
    return tuple(layout_block(row) for row in rows) if rows else default_layout(main_prompt=SEED_MAIN_PROMPT)


async def create_prompt_layout(name: str, copy_from: str | None = None) -> PromptLayoutRow:
    """A new layout seeded with the built-in order, or a copy of *copy_from*'s blocks."""
    name = name.strip()
    if not name:
        raise PromptLayoutError("A layout needs a name.")
    layout_id = uuid.uuid4().hex
    if copy_from is not None:
        source = await get_prompt_blocks(copy_from)
        if not source:
            raise PromptLayoutError("The layout to copy does not exist.")
        blocks = [
            layout_block(
                {**row, "id": marker_block_id(layout_id, row["kind"]) if row["kind"] != CUSTOM_KIND else uuid.uuid4().hex}
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
    if await get_prompt_layout(layout_id) is None:
        return False
    if layout_id == await get_active_prompt_layout_id():
        raise PromptLayoutError("The active layout cannot be deleted.")
    if len(await list_prompt_layouts()) <= 1:
        raise PromptLayoutError("The last layout cannot be deleted.")
    async with get_db() as db:
        await db.execute("DELETE FROM prompt_layouts WHERE id = ?", (layout_id,))
        await db.commit()
    return True


async def set_active_prompt_layout(layout_id: str) -> bool:
    if await get_prompt_layout(layout_id) is None:
        return False
    async with get_db() as db:
        await db.execute("UPDATE settings SET active_prompt_layout_id = ? WHERE id = 1", (layout_id,))
        await db.commit()
    return True


def _clean_role(role: Any) -> str:
    if role not in _ROLES:
        raise PromptLayoutError("Role must be system, user or assistant.")
    return str(role)


def _clean_region(region: Any) -> str:
    if region not in _REGIONS:
        raise PromptLayoutError("Region must be prefix or writer.")
    return str(region)


async def create_prompt_block(layout_id: str, data: Mapping[str, Any]) -> PromptBlockRow:
    """A custom block at the end of its region; the Writer request unless *data* says otherwise."""
    if await get_prompt_layout(layout_id) is None:
        raise LookupError(layout_id)
    region = _clean_region(data.get("region", "writer"))
    role = _clean_role(data.get("role", "system")) if region == "prefix" else "system"
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
            label=str(data.get("label", "")).strip(),
            content=str(data.get("content", "")),
            role=cast(LayoutRole, role),
            region=cast(LayoutRegion, region),
            sort_order=int(rows[0]["top"]) + 1,
            enabled=bool(data.get("enabled", True)),
        )
        await insert_layout_blocks(db, layout_id, [block])
        await db.commit()
    row = await get_prompt_block(block_id)
    assert row is not None
    return row


async def update_prompt_block(block_id: str, data: Mapping[str, Any]) -> PromptBlockRow | None:
    """Edit a block. A marker keeps its name, switch and side of the chat history; only the Main prompt's text is editable, and
    only blocks above the chat history take a role."""
    row = await get_prompt_block(block_id)
    if row is None:
        return None
    custom = row["kind"] == CUSTOM_KIND
    updates: dict[str, Any] = {}
    if "content" in data:
        if not custom and row["kind"] != "main_prompt":
            raise PromptLayoutError("A built-in block cannot be edited.")
        updates["content"] = str(data["content"])
    if "label" in data:
        if not custom:
            raise PromptLayoutError("A built-in block keeps its name.")
        updates["label"] = str(data["label"]).strip()
    if "enabled" in data:
        if not custom:
            raise PromptLayoutError("Another setting switches this block.")
        updates["enabled"] = 1 if data["enabled"] else 0
    if "region" in data:
        region = _clean_region(data["region"])
        if region != row["region"] and not custom:
            raise PromptLayoutError("This block cannot cross the chat history.")
        updates["region"] = region
    if "role" in data:
        role = _clean_role(data["role"])
        if role != row["role"] and not custom and marker_region(row["kind"]) != "prefix":
            raise PromptLayoutError("Roles apply only above the chat history.")
        updates["role"] = role
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


async def reorder_prompt_blocks(layout_id: str, prefix: Sequence[str], writer: Sequence[str]) -> list[PromptBlockRow]:
    """Place every block of the layout: *prefix* ids above the chat history, *writer* ids below, each in order."""
    rows = {row["id"]: row for row in await get_prompt_blocks(layout_id)}
    if not rows:
        raise LookupError(layout_id)
    placed = [*prefix, *writer]
    if len(placed) != len(set(placed)) or set(placed) != set(rows):
        raise PromptLayoutError("The order must list every block of the layout exactly once.")
    for region, ids in (("prefix", prefix), ("writer", writer)):
        for block_id in ids:
            kind = rows[block_id]["kind"]
            if kind in MARKERS and marker_region(kind) != region:
                raise PromptLayoutError("This block cannot cross the chat history.")
    async with get_db() as db:
        await db.executemany(
            "UPDATE prompt_blocks SET sort_order = ?, region = ? WHERE id = ?",
            [
                (index, region, block_id)
                for index, (region, block_id) in enumerate([("prefix", i) for i in prefix] + [("writer", i) for i in writer])
            ],
        )
        await db.commit()
    return await get_prompt_blocks(layout_id)
