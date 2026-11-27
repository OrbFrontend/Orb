"""The phrase bank, each group a row of one profile. Without a ``profile_id`` every function acts on the active profile."""

from __future__ import annotations

import json
from typing import cast

from ..connection import get_db, select_rows
from ..models import PhraseBankRow, PhraseGroup
from .profiles import get_active_profile_id


def _row_to_group(row) -> PhraseGroup:
    """Normalise a DB row into a phrase-bank group dict for the detector."""
    if (row["kind"] or "literal") == "regex":
        return {"kind": "regex", "pattern": row["pattern"] or ""}
    return {"kind": "literal", "variants": json.loads(row["variants"])}


async def get_phrase_bank(profile_id: str | None = None) -> list[PhraseGroup]:
    """Return phrase bank groups (literal or regex) for the slop detector."""
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows(
        "SELECT variants, kind, pattern FROM phrase_bank WHERE profile_id = ? ORDER BY id ASC", (profile_id,)
    )
    return [_row_to_group(r) for r in rows]


async def get_phrase_bank_rows(profile_id: str | None = None) -> list[PhraseBankRow]:
    """Return phrase bank rows with ids for UI management."""
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows(
        "SELECT id, variants, kind, pattern FROM phrase_bank WHERE profile_id = ? ORDER BY id ASC", (profile_id,)
    )
    return [
        cast(
            PhraseBankRow,
            {
                "id": r["id"],
                "kind": r["kind"] or "literal",
                "variants": json.loads(r["variants"]),
                "pattern": r["pattern"] or "",
            },
        )
        for r in rows
    ]


async def add_phrase_group(variants: list[str], kind: str = "literal", pattern: str = "", profile_id: str | None = None) -> int:
    """Add a new phrase group (literal variants or a single regex). Returns the new row id."""
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO phrase_bank (profile_id, variants, kind, pattern) VALUES (?, ?, ?, ?)",
            (profile_id, json.dumps(variants), kind, pattern or None),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid


async def update_phrase_group(
    group_id: int, variants: list[str], kind: str = "literal", pattern: str = "", profile_id: str | None = None
) -> bool:
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE phrase_bank SET variants = ?, kind = ?, pattern = ? WHERE id = ? AND profile_id = ?",
            (json.dumps(variants), kind, pattern or None, group_id, profile_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_phrase_group(group_id: int, profile_id: str | None = None) -> bool:
    profile_id = profile_id or await get_active_profile_id()
    async with get_db() as db:
        cur = await db.execute("DELETE FROM phrase_bank WHERE id = ? AND profile_id = ?", (group_id, profile_id))
        await db.commit()
        return cur.rowcount > 0
