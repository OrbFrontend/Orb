"""A profile's prompt layout: its ordered blocks, each a row of one profile. Without a ``profile_id`` every function acts on the
active profile. ``queries.profiles`` owns the profiles themselves."""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, cast

import aiosqlite

from ...core.prompt_layout import (
    CUSTOM_KIND,
    EDITABLE_MARKERS,
    LayoutBlock,
    LayoutRegion,
    LayoutRole,
    default_role,
    marker_text_problem,
)
from ..connection import check_expected, get_db, select_rows
from ..models import PromptBlockRow
from .active_profile import get_active_profile_id

_BLOCK_COLUMNS = "profile_id, id, kind, label, content, role, region, sort_order, enabled"


class PromptLayoutError(ValueError):
    """A layout edit the rules refuse; the message is safe to show."""


async def insert_layout_blocks(db: aiosqlite.Connection, profile_id: str, blocks: Sequence[LayoutBlock]) -> None:
    await db.executemany(
        f"INSERT INTO prompt_blocks ({_BLOCK_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",  # nosec B608 -- fixed columns
        [(profile_id, b.id, b.kind, b.label, b.content, b.role, b.region, b.sort_order, int(b.enabled)) for b in blocks],
    )


async def get_prompt_blocks(profile_id: str | None = None) -> list[PromptBlockRow]:
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows(
        f"SELECT {_BLOCK_COLUMNS} FROM prompt_blocks WHERE profile_id = ? ORDER BY sort_order, rowid",  # nosec B608
        (profile_id,),
    )
    return [cast(PromptBlockRow, dict(row)) for row in rows]


async def get_prompt_block(block_id: str, profile_id: str | None = None) -> PromptBlockRow | None:
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows(
        f"SELECT {_BLOCK_COLUMNS} FROM prompt_blocks WHERE profile_id = ? AND id = ?",  # nosec B608 -- fixed columns
        (profile_id, block_id),
    )
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


async def create_prompt_block(data: Mapping[str, Any], profile_id: str | None = None) -> PromptBlockRow:
    """A custom block at the end of the profile's layout."""
    profile_id = profile_id or await get_active_profile_id()
    block_id = uuid.uuid4().hex
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT COALESCE(MAX(sort_order), -1) AS top FROM prompt_blocks WHERE profile_id = ?", (profile_id,)
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
        await insert_layout_blocks(db, profile_id, [block])
        await db.commit()
    row = await get_prompt_block(block_id, profile_id)
    assert row is not None
    return row


async def update_prompt_block(
    block_id: str, data: Mapping[str, Any], profile_id: str | None = None, *, expected: Mapping[str, Any] | None = None
) -> PromptBlockRow | None:
    """Edit a block's label, text, role or switch. A marker keeps its name, only an editable marker's text changes, keeping its
    placeholder. :func:`reorder_prompt_blocks` moves blocks.

    With *expected*, the named columns are compared and the update written in one transaction, raising ``StaleWrite`` if any
    changed since the caller read them."""
    profile_id = profile_id or await get_active_profile_id()
    row = await get_prompt_block(block_id, profile_id)
    if row is None:
        return None
    kind = row["kind"]
    custom = kind == CUSTOM_KIND
    if "content" in data:
        if not custom and kind not in EDITABLE_MARKERS:
            raise PromptLayoutError("A built-in block cannot be edited.")
        if problem := marker_text_problem(kind, row["label"], data["content"]):
            raise PromptLayoutError(problem)
    if not custom and "label" in data:
        raise PromptLayoutError("A built-in block keeps its name.")
    updates = {column: data[column] for column in ("label", "content", "role", "enabled") if column in data}
    if "label" in updates:
        updates["label"] = updates["label"].strip()
    if "enabled" in updates:
        updates["enabled"] = int(updates["enabled"])
    if updates or expected is not None:
        async with get_db() as db:
            owner = {"profile_id": profile_id}
            if expected is not None and not await check_expected(db, "prompt_blocks", block_id, expected, owner=owner):
                return None
            if updates:
                sets = ", ".join(f"{column} = ?" for column in updates)
                await db.execute(
                    f"UPDATE prompt_blocks SET {sets} WHERE profile_id = ? AND id = ?",  # nosec B608 -- columns from the fixed set above
                    [*updates.values(), profile_id, block_id],
                )
            # Also ends the transaction a comparison opened.
            await db.commit()
    return await get_prompt_block(block_id, profile_id)


async def delete_prompt_block(block_id: str, profile_id: str | None = None) -> bool:
    profile_id = profile_id or await get_active_profile_id()
    row = await get_prompt_block(block_id, profile_id)
    if row is None:
        return False
    if row["kind"] != CUSTOM_KIND:
        raise PromptLayoutError("A built-in block cannot be deleted.")
    async with get_db() as db:
        await db.execute("DELETE FROM prompt_blocks WHERE profile_id = ? AND id = ?", (profile_id, block_id))
        await db.commit()
    return True


def _crossed_role(row: PromptBlockRow, region: LayoutRegion) -> LayoutRole:
    """*row*'s role once placed in *region*."""
    if row["region"] != region and row["role"] == default_role(row["region"]):
        return default_role(region)
    return row["role"]


async def reorder_prompt_blocks(
    prefix: Sequence[str], writer: Sequence[str], profile_id: str | None = None
) -> list[PromptBlockRow]:
    """Place every block of the layout: *prefix* ids above the chat history, *writer* ids below, each in order. A block that
    crosses the history with its region's default role takes the new region's default; any other role stays."""
    profile_id = profile_id or await get_active_profile_id()
    rows = {row["id"]: row for row in await get_prompt_blocks(profile_id)}
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
            "UPDATE prompt_blocks SET sort_order = ?, region = ?, role = ? WHERE profile_id = ? AND id = ?",
            [
                (index, region, _crossed_role(rows[block_id], region), profile_id, block_id)
                for index, (region, block_id) in enumerate(placed)
            ],
        )
        await db.commit()
    return await get_prompt_blocks(profile_id)
