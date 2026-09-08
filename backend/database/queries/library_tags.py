"""Database queries for the tag vocabulary and auto-tagged card state."""

from __future__ import annotations

import json

from ..connection import get_db


async def get_vocabulary() -> list[str]:
    """The curated tag names, in the order the user arranged them."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT name FROM library_tags ORDER BY position, name"))
        return [str(r["name"]) for r in rows]


async def set_vocabulary(names: list[str]) -> None:
    """Replace the normalized vocabulary in one transaction."""
    async with get_db() as db:
        await db.execute("DELETE FROM library_tags")
        await db.executemany(
            "INSERT INTO library_tags (name, position) VALUES (?, ?)",
            [(name, i) for i, name in enumerate(names)],
        )
        await db.commit()


# A card is pending when its vocabulary stamp or content timestamp is stale.
_PENDING_WHERE = "WHERE auto_tag_vocab_hash != ? OR auto_tag_card_updated_at != updated_at"


async def count_library_cards() -> int:
    """Return the number of cards in the library."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT COUNT(*) AS n FROM character_cards"))
        return int(rows[0]["n"]) if rows else 0


async def count_tagged_cards() -> int:
    """Return the number of cards whose tags the tagger may prune."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT COUNT(*) AS n FROM character_cards WHERE auto_tag_vocab_hash != ''"))
        return int(rows[0]["n"]) if rows else 0


async def count_pending_auto_tags(vocab_hash: str) -> int:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT COUNT(*) AS n FROM character_cards {_PENDING_WHERE}",  # nosec B608 -- literal fragment
                (vocab_hash,),
            )
        )
        return int(rows[0]["n"]) if rows else 0


async def list_pending_auto_tag_ids(vocab_hash: str) -> list[str]:
    """Return pending card IDs, newest first."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT id FROM character_cards {_PENDING_WHERE} ORDER BY created_at DESC",  # nosec B608
                (vocab_hash,),
            )
        )
        return [str(r["id"]) for r in rows]


async def apply_auto_tags(card_id: str, tags: list[str], vocab_hash: str, card_updated_at: str) -> None:
    """Commit one card's tags and freshness stamps."""
    async with get_db() as db:
        await db.execute(
            "UPDATE character_cards SET tags = ?, auto_tag_vocab_hash = ?, auto_tag_card_updated_at = ? WHERE id = ?",
            (json.dumps(tags), vocab_hash, card_updated_at, card_id),
        )
        await db.commit()


async def prune_auto_tags(removed: set[str]) -> int:
    """Remove deleted vocabulary names from cards previously tagged by a run."""
    if not removed:
        return 0
    lowered = {t.lower() for t in removed}
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT id, tags FROM character_cards WHERE auto_tag_vocab_hash != ''"))
        updates: list[tuple[str, str]] = []
        for r in rows:
            try:
                tags = json.loads(r["tags"]) if r["tags"] else []
            except json.JSONDecodeError:
                tags = []
            if not isinstance(tags, list):
                tags = []
            kept = [str(t) for t in tags if str(t).lower() not in lowered]
            if len(kept) != len(tags):
                updates.append((json.dumps(kept), str(r["id"])))
        if updates:
            await db.executemany("UPDATE character_cards SET tags = ? WHERE id = ?", updates)
            await db.commit()
        return len(updates)


async def bump_auto_tag_vocab_hash(new_hash: str) -> None:
    """Restamp cards already handled by the tagger."""
    async with get_db() as db:
        await db.execute("UPDATE character_cards SET auto_tag_vocab_hash = ? WHERE auto_tag_vocab_hash != ''", (new_hash,))
        await db.commit()
