from __future__ import annotations

import json

from ..connection import get_db
from ..models import PhraseGroup


def _row_to_group(row) -> PhraseGroup:
    """Normalise a DB row into a phrase-bank group dict for the detector."""
    if (row["kind"] or "literal") == "regex":
        return {"kind": "regex", "pattern": row["pattern"] or ""}
    return {"kind": "literal", "variants": json.loads(row["variants"])}


async def get_phrase_bank() -> list[PhraseGroup]:
    """Return phrase bank groups (literal or regex) for the slop detector."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT variants, kind, pattern FROM phrase_bank ORDER BY id ASC"))
        return [_row_to_group(r) for r in rows]


async def get_phrase_bank_rows() -> list[dict]:
    """Return phrase bank rows with ids for UI management."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT id, variants, kind, pattern FROM phrase_bank ORDER BY id ASC"))
        return [
            {
                "id": r["id"],
                "kind": r["kind"] or "literal",
                "variants": json.loads(r["variants"]),
                "pattern": r["pattern"] or "",
            }
            for r in rows
        ]


async def add_phrase_group(
    variants: list[str],
    kind: str = "literal",
    pattern: str = "",
) -> int:
    """Add a new phrase group (literal variants or a single regex). Returns the new row id."""
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO phrase_bank (variants, kind, pattern) VALUES (?, ?, ?)",
            (json.dumps(variants), kind, pattern or None),
        )
        await db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid


async def update_phrase_group(
    group_id: int,
    variants: list[str],
    kind: str = "literal",
    pattern: str = "",
) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE phrase_bank SET variants = ?, kind = ?, pattern = ? WHERE id = ?",
            (json.dumps(variants), kind, pattern or None, group_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def delete_phrase_group(group_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM phrase_bank WHERE id = ?", (group_id,))
        await db.commit()
        return cur.rowcount > 0
