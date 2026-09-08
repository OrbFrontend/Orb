"""Database queries for the tag vocabulary and auto-tagged card state."""

from __future__ import annotations

import json

from ..connection import get_db, immediate_tx


class VocabularyConflict(RuntimeError):
    """The vocabulary changed after the caller read it."""


async def get_vocabulary() -> list[str]:
    """The curated tag names, in the order the user arranged them."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT name FROM library_tags ORDER BY position, name"))
        return [str(r["name"]) for r in rows]


async def replace_vocabulary(names: list[str], *, expected: list[str], new_hash: str) -> int:
    """Atomically replace the vocabulary and reconcile owned card tags.

    Returns the number of cards whose visible tag list changed.  ``expected`` is
    checked under SQLite's write lock so a second process or stale browser cannot
    overwrite a vocabulary it did not read.
    """
    async with immediate_tx() as db:
        rows = list(await db.execute_fetchall("SELECT name FROM library_tags ORDER BY position, name"))
        current = [str(row["name"]) for row in rows]
        if current != expected:
            raise VocabularyConflict("The tag vocabulary changed in another window")

        old_map = {name.lower(): name for name in current}
        new_map = {name.lower(): name for name in names}
        added = new_map.keys() - old_map.keys()
        removed = old_map.keys() - new_map.keys()
        recased = {key for key in old_map.keys() & new_map.keys() if old_map[key] != new_map[key]}

        if current != names:
            await db.execute("DELETE FROM library_tags")
            await db.executemany(
                "INSERT INTO library_tags (name, position) VALUES (?, ?)",
                [(name, i) for i, name in enumerate(names)],
            )

        updates: list[tuple[str, str]] = []
        if removed or recased:
            tagged_rows = list(
                await db.execute_fetchall("SELECT id, tags FROM character_cards WHERE auto_tag_vocab_hash != ''")
            )
            for row in tagged_rows:
                try:
                    tags = json.loads(row["tags"]) if row["tags"] else []
                except json.JSONDecodeError:
                    continue
                if not isinstance(tags, list):
                    continue
                reconciled: list[object] = []
                changed = False
                for tag in tags:
                    if not isinstance(tag, str):
                        reconciled.append(tag)
                        continue
                    key = tag.lower()
                    if key in removed:
                        changed = True
                        continue
                    canonical = new_map.get(key)
                    if canonical is not None and canonical != tag:
                        reconciled.append(canonical)
                        changed = True
                    else:
                        reconciled.append(tag)
                if changed:
                    updates.append((json.dumps(reconciled), str(row["id"])))
            if updates:
                await db.executemany("UPDATE character_cards SET tags = ? WHERE id = ?", updates)

        # Removing/reordering/recasing cannot introduce a newly applicable tag.
        # Repair only mismatched stamps so a no-op save does not rewrite N rows.
        if not added:
            await db.execute(
                "UPDATE character_cards SET auto_tag_vocab_hash = ? "
                "WHERE auto_tag_vocab_hash != '' AND auto_tag_vocab_hash != ?",
                (new_hash, new_hash),
            )
        return len(updates)


# A card is pending when its vocabulary stamp or content timestamp is stale.
_PENDING_WHERE = "WHERE auto_tag_vocab_hash != ? OR auto_tag_card_updated_at != updated_at"


async def get_auto_tag_counts(vocab_hash: str | None) -> dict[str, int]:
    """Return the manager counts in one card-table scan and one snapshot."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT COUNT(*) AS total, "
                "COALESCE(SUM(CASE WHEN auto_tag_vocab_hash != '' THEN 1 ELSE 0 END), 0) AS tagged, "
                "COALESCE(SUM(CASE WHEN ? IS NOT NULL AND "
                "(auto_tag_vocab_hash != ? OR auto_tag_card_updated_at != updated_at) THEN 1 ELSE 0 END), 0) AS pending "
                "FROM character_cards",
                (vocab_hash, vocab_hash),
            )
        )
        if not rows:
            return {"total": 0, "tagged": 0, "pending": 0}
        return {key: int(rows[0][key]) for key in ("total", "tagged", "pending")}


async def list_pending_auto_tag_ids(vocab_hash: str, *, force: bool = False) -> list[str]:
    """Return pending (or, when forced, all) card IDs, newest first."""
    async with get_db() as db:
        where = "" if force else _PENDING_WHERE
        params = () if force else (vocab_hash,)
        rows = list(
            await db.execute_fetchall(
                f"SELECT id FROM character_cards {where} ORDER BY created_at DESC",  # nosec B608
                params,
            )
        )
        return [str(r["id"]) for r in rows]


async def apply_auto_tags(card_id: str, tags: list[str], vocab_hash: str, card_updated_at: str) -> bool:
    """Commit tags only if the card still matches the model input snapshot."""
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE character_cards SET tags = ?, auto_tag_vocab_hash = ?, auto_tag_card_updated_at = ? "
            "WHERE id = ? AND updated_at = ?",
            (json.dumps(tags), vocab_hash, card_updated_at, card_id, card_updated_at),
        )
        await db.commit()
        return cursor.rowcount == 1
