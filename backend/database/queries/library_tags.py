"""The Character Library's curated tag vocabulary and the auto-tagger's writes.

A card has one set of tags, and a run overwrites it. ``character_cards.tags`` is
the same column an import fills, the card editor shows, and an export ships, so
there is nothing downstream to merge and nothing that reads a different list
than the one the user is looking at. The tags a card was imported with are gone
once a run has touched it — see 0061, and the confirmation the Manager panel
puts in front of the run.

The two ``auto_tag_*`` stamps beside the column are bookkeeping, not a second
opinion: they say which vocabulary produced the current contents and what the
card looked like at the time.
"""

from __future__ import annotations

import json

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


# A card needs (re-)tagging when it was tagged against a different vocabulary or
# has been edited since. A never-tagged card falls out of the same test for free:
# its hash defaults to '', which no sha256 digest equals.
#
# ``updated_at`` is the content fingerprint, and ``apply_auto_tags`` deliberately
# does not bump it — a run that touched it would invalidate its own work on the
# next comparison and reshuffle a library sorted by recency into tagging order.
#
# Hand-editing a card's tags in the card editor therefore makes it pending like
# any other edit, and the next run overwrites what was typed. That follows from
# there being one tag list rather than two, and it is the intended trade — the
# run announces it up front rather than keeping a copy nobody asked for.
_PENDING_WHERE = "WHERE auto_tag_vocab_hash != ? OR auto_tag_card_updated_at != updated_at"


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
                f"SELECT COUNT(*) AS n FROM character_cards {_PENDING_WHERE}",  # nosec B608 -- literal fragment
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
                f"SELECT id FROM character_cards {_PENDING_WHERE} ORDER BY created_at DESC",  # nosec B608
                (vocab_hash,),
            )
        )
        return [str(r["id"]) for r in rows]


async def apply_auto_tags(card_id: str, tags: list[str], vocab_hash: str, card_updated_at: str) -> None:
    """Commit one card's answer over its tags. Called per card so a cancelled run keeps its work.

    *card_updated_at* is the value the caller already read off the card, written
    back verbatim: pairing the stamp with the row's own ``updated_at`` is what
    makes a later edit — and only a later edit — pending again.
    """
    async with get_db() as db:
        await db.execute(
            "UPDATE character_cards SET tags = ?, auto_tag_vocab_hash = ?, auto_tag_card_updated_at = ? WHERE id = ?",
            (json.dumps(tags), vocab_hash, card_updated_at, card_id),
        )
        await db.commit()


async def prune_auto_tags(removed: set[str]) -> int:
    """Strip deleted vocabulary tags from every tagged card. No model calls.

    Returns the number of rows changed. Matching is case-insensitive because the
    vocabulary itself dedupes that way, so a tag deleted as ``Fantasy`` must also
    remove a stored ``fantasy``.

    Scoped to cards the tagger has written, so deleting a vocabulary tag cannot
    reach into the tags of a card no run has ever touched — those are still the
    creator's, and only a run is allowed to replace them.
    """
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
    """Mark every tagged card current against *new_hash* without re-running.

    The edit that earns this is one that removed or reordered tags: no card can
    gain a tag it was never offered, so every stored answer is still the answer
    the new vocabulary would produce. Never-tagged cards are excluded, or the
    stamp would claim work that was never done.
    """
    async with get_db() as db:
        await db.execute("UPDATE character_cards SET auto_tag_vocab_hash = ? WHERE auto_tag_vocab_hash != ''", (new_hash,))
        await db.commit()
