"""The Character Library's curated tag vocabulary and its auto-tag assignments.

Both tables are owned by the auto-tagger slice and read by nothing else, so the
read and the write path live together here instead of splitting across the
shared settings cascade. Imported ``character_cards.tags`` are never written by
anything in this module — an auto tag is a separate, regenerable assertion about
a card, and conflating the two would make a tagging run destroy authored data.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from ..connection import get_db


async def get_vocabulary() -> list[str]:
    """The curated tag names, in the order the user arranged them."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT name FROM library_tags ORDER BY position, name"))
        return [str(r["name"]) for r in rows]


async def set_vocabulary(names: list[str]) -> None:
    """Replace the vocabulary with *names* (already normalized by the caller).

    Delete-then-insert rather than a diff: ``position`` is the whole ordering
    contract and a reorder rewrites every row anyway, so the simple form is also
    the correct one. Both statements share one transaction, so a reader never
    observes an empty vocabulary mid-save.
    """
    async with get_db() as db:
        await db.execute("DELETE FROM library_tags")
        await db.executemany(
            "INSERT INTO library_tags (name, position) VALUES (?, ?)",
            [(name, i) for i, name in enumerate(names)],
        )
        await db.commit()


# A card needs (re-)tagging when it has never been evaluated, was evaluated
# against a different vocabulary, or has been edited since. ``updated_at`` is a
# free content fingerprint: ``update_character_card`` is its only writer, and a
# tagging run writes to ``character_auto_tags`` instead, so a run never
# invalidates its own work.
_PENDING_WHERE = """
    LEFT JOIN character_auto_tags a ON a.character_card_id = c.id
    WHERE a.vocab_hash IS NULL OR a.vocab_hash != ? OR a.card_updated_at != c.updated_at
"""


async def count_library_cards() -> int:
    """How many cards the tagger is responsible for.

    Here rather than off ``list_character_cards`` because the panel needs the
    number, not the rows: counting 406 cards by materializing them is the sort of
    waste that only shows up once a library is large.
    """
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT COUNT(*) AS n FROM character_cards"))
        return int(rows[0]["n"]) if rows else 0


async def count_pending_auto_tags(vocab_hash: str) -> int:
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT COUNT(*) AS n FROM character_cards c {_PENDING_WHERE}",  # nosec B608 -- literal fragment
                (vocab_hash,),
            )
        )
        return int(rows[0]["n"]) if rows else 0


async def list_pending_auto_tag_ids(vocab_hash: str) -> list[str]:
    """Ids only, newest card first — the run streams each card's body itself.

    Ordering is newest-first so a user who cancels an early run has tagged the
    cards they most recently imported, which are the ones they are looking for.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT c.id FROM character_cards c {_PENDING_WHERE} ORDER BY c.created_at DESC",  # nosec B608
                (vocab_hash,),
            )
        )
        return [str(r["id"]) for r in rows]


async def get_auto_tag_assignments() -> dict[str, list[str]]:
    """``{card_id: [tags]}`` for every tagged card, for the library's filter."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT character_card_id, tags FROM character_auto_tags"))
        out: dict[str, list[str]] = {}
        for r in rows:
            try:
                tags = json.loads(r["tags"]) if r["tags"] else []
            except json.JSONDecodeError:
                continue
            if isinstance(tags, list):
                out[str(r["character_card_id"])] = [str(t) for t in tags]
        return out


async def upsert_auto_tags(card_id: str, tags: list[str], vocab_hash: str, card_updated_at: str) -> None:
    """Commit one card's answer. Called per card so a cancelled run keeps its work."""
    async with get_db() as db:
        await db.execute(
            "INSERT INTO character_auto_tags (character_card_id, tags, vocab_hash, card_updated_at, tagged_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(character_card_id) DO UPDATE SET "
            "tags = excluded.tags, vocab_hash = excluded.vocab_hash, "
            "card_updated_at = excluded.card_updated_at, tagged_at = excluded.tagged_at",
            (card_id, json.dumps(tags), vocab_hash, card_updated_at, datetime.now(UTC).isoformat()),
        )
        await db.commit()


async def prune_auto_tags(removed: set[str]) -> int:
    """Strip deleted vocabulary tags from every assignment. No model calls.

    Returns the number of rows changed. Matching is case-insensitive because the
    vocabulary itself dedupes that way, so a tag deleted as ``Fantasy`` must also
    remove a stored ``fantasy``.
    """
    if not removed:
        return 0
    lowered = {t.lower() for t in removed}
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT character_card_id, tags FROM character_auto_tags"))
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
                updates.append((json.dumps(kept), str(r["character_card_id"])))
        if updates:
            await db.executemany("UPDATE character_auto_tags SET tags = ? WHERE character_card_id = ?", updates)
            await db.commit()
        return len(updates)


async def bump_auto_tag_vocab_hash(new_hash: str) -> None:
    """Mark every assignment current against *new_hash* without re-running.

    The edit that earns this is one that removed or reordered tags: no card can
    gain a tag it was never offered, so every stored answer is still the answer
    the new vocabulary would produce.
    """
    async with get_db() as db:
        await db.execute("UPDATE character_auto_tags SET vocab_hash = ?", (new_hash,))
        await db.commit()
