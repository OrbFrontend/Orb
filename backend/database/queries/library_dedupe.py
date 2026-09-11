"""Database operations for the Character Library duplicate finder.

The matching itself lives in :mod:`backend.features.library_dedupe`; this
module owns only the intentionally narrow persistence boundary.  In particular,
avatars are read one card at a time because their base64 blobs are large enough
that loading a stale library in a single query can consume gigabytes of memory.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from ..connection import get_db, immediate_tx


async def list_cards_for_dedupe() -> list[dict[str, Any]]:
    """Return the server-only card bodies and cached avatar values for a scan.

    ``created_at`` and ``has_avatar`` are not matcher inputs.  They exist so the
    review UI can tell identically named copies apart, which is the one thing a
    name and an id cannot do.  ``avatar_b64 IS NOT NULL`` reads the record header
    rather than the blob, so it stays out of the memory budget this module's
    one-avatar-at-a-time rule protects.
    """
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, name, description, personality, scenario, first_mes, mes_example, "
                "system_prompt, post_history_instructions, alternate_greetings, creator, "
                "avatar_dhash, avatar_dhash_stamp, created_at, updated_at, "
                "avatar_b64 IS NOT NULL AS has_avatar "
                "FROM character_cards ORDER BY id"
            )
        )
    cards: list[dict[str, Any]] = []
    for row in rows:
        card = dict(row)
        try:
            card["alternate_greetings"] = json.loads(card["alternate_greetings"]) if card["alternate_greetings"] else []
        except json.JSONDecodeError:
            card["alternate_greetings"] = []
        cards.append(card)
    return cards


async def list_stale_avatar_ids(stamp_prefix: str) -> list[str]:
    """Return cards whose avatar cache does not match this revision and edit."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id FROM character_cards WHERE avatar_dhash_stamp != ? || updated_at ORDER BY id",
                (stamp_prefix,),
            )
        )
    return [str(row["id"]) for row in rows]


async def read_avatar_b64(card_id: str) -> tuple[str | None, str, str] | None:
    """Read one avatar blob with its immutable-for-CAS snapshot and display name."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT avatar_b64, updated_at, name FROM character_cards WHERE id = ?",
                (card_id,),
            )
        )
    if not rows:
        return None
    row = rows[0]
    return row["avatar_b64"], str(row["updated_at"]), str(row["name"])


async def apply_avatar_dhash(card_id: str, dhash: str, stamp: str) -> bool:
    """Store a dHash only if the avatar's card snapshot remains current.

    ``updated_at`` is deliberately absent from ``SET``.  Updating it here would
    make an otherwise current auto-tag stamp stale for every scanned card.
    """
    _, separator, updated_at = stamp.partition(":")
    if not separator or not updated_at:
        raise ValueError("Avatar dHash stamps must contain a revision and updated_at")
    async with get_db() as db:
        cursor = await db.execute(
            "UPDATE character_cards SET avatar_dhash = ?, avatar_dhash_stamp = ? WHERE id = ? AND updated_at = ?",
            (dhash, stamp, card_id, updated_at),
        )
        await db.commit()
    return cursor.rowcount == 1


async def get_dismissals() -> dict[tuple[str, str], tuple[str, str]]:
    """Return the body-hash stamps for every currently remembered pair."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT card_a, card_b, hash_a, hash_b FROM duplicate_dismissals"))
    return {(str(row["card_a"]), str(row["card_b"])): (str(row["hash_a"]), str(row["hash_b"])) for row in rows}


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    if not a or not b or a == b:
        raise ValueError("A dismissal must name two different cards")
    return (a, b) if a < b else (b, a)


async def add_dismissals(pairs: Iterable[tuple[str, str, str, str]]) -> int:
    """Upsert pairs with their current body-hash stamps and return their count."""
    values: list[tuple[str, str, str, str, str]] = []
    now = datetime.now(UTC).isoformat()
    for a, b, hash_a, hash_b in pairs:
        left, right = _canonical_pair(a, b)
        if (left, right) != (a, b):
            hash_a, hash_b = hash_b, hash_a
        values.append((left, right, hash_a, hash_b, now))
    if not values:
        return 0
    async with immediate_tx() as db:
        await db.executemany(
            "INSERT INTO duplicate_dismissals (card_a, card_b, hash_a, hash_b, dismissed_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(card_a, card_b) DO UPDATE SET hash_a = excluded.hash_a, hash_b = excluded.hash_b, "
            "dismissed_at = excluded.dismissed_at",
            values,
        )
    return len(values)


async def remove_dismissals(pairs: Iterable[tuple[str, str]]) -> int:
    """Forget dismissal pairs.  This is the duplicate finder's reversible action."""
    values = [_canonical_pair(a, b) for a, b in pairs]
    if not values:
        return 0
    async with immediate_tx() as db:
        cursor = await db.executemany(
            "DELETE FROM duplicate_dismissals WHERE card_a = ? AND card_b = ?",
            values,
        )
    return cursor.rowcount


async def get_relink_impact(from_id: str, to_id: str) -> dict[str, int]:
    """Count a removal's conversation impact before the caller applies it."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                """SELECT
                     (SELECT COUNT(*) FROM conversations WHERE character_card_id = ?) AS solo,
                     (SELECT COUNT(DISTINCT conversation_id) FROM group_members WHERE character_card_id = ?) AS groups,
                     (SELECT COUNT(DISTINCT conversation_id)
                      FROM (
                        SELECT id AS conversation_id FROM conversations WHERE character_card_id = ?
                        UNION
                        SELECT conversation_id FROM group_members WHERE character_card_id = ?
                      )) AS conversations,
                     (SELECT COUNT(DISTINCT doomed.conversation_id)
                      FROM group_members AS doomed
                      JOIN group_members AS keeper
                        ON keeper.conversation_id = doomed.conversation_id
                       AND keeper.character_card_id = ?
                       AND keeper.active = 1
                      WHERE doomed.character_card_id = ?) AS collisions""",
                (from_id, from_id, from_id, from_id, to_id, from_id),
            )
        )
    row = rows[0]
    return {key: int(row[key]) for key in ("solo", "groups", "conversations", "collisions")}


async def relink_card(from_id: str, to_id: str) -> dict[str, int]:
    """Move a doomed card's conversations and group slots to a keeper.

    The group-member unique index permits only one active card per conversation.
    If both cards are already active in a group, the keeper already has a
    speaking slot, so the doomed row is removed instead of triggering an
    integrity error.  Every rewrite happens under one immediate transaction.
    """
    if from_id == to_id:
        raise ValueError("A card cannot be relinked to itself")
    async with immediate_tx() as db:
        keeper_rows = list(
            await db.execute_fetchall(
                "SELECT name, scenario, post_history_instructions FROM character_cards WHERE id = ?",
                (to_id,),
            )
        )
        if not keeper_rows:
            raise ValueError("Keeper card not found")
        keeper = keeper_rows[0]

        impact_rows = list(
            await db.execute_fetchall(
                """SELECT
                     (SELECT COUNT(*) FROM conversations WHERE character_card_id = ?) AS solo,
                     (SELECT COUNT(DISTINCT conversation_id) FROM group_members WHERE character_card_id = ?) AS groups,
                     (SELECT COUNT(DISTINCT conversation_id)
                      FROM (
                        SELECT id AS conversation_id FROM conversations WHERE character_card_id = ?
                        UNION
                        SELECT conversation_id FROM group_members WHERE character_card_id = ?
                      )) AS conversations""",
                (from_id, from_id, from_id, from_id),
            )
        )
        impact = {key: int(impact_rows[0][key]) for key in ("solo", "groups", "conversations")}

        await db.execute("UPDATE conversations SET character_card_id = ? WHERE character_card_id = ?", (to_id, from_id))

        member_rows = list(
            await db.execute_fetchall(
                "SELECT id, conversation_id FROM group_members WHERE character_card_id = ? ORDER BY id",
                (from_id,),
            )
        )
        collisions = 0
        for member in member_rows:
            keeper_exists = await db.execute_fetchall(
                "SELECT 1 FROM group_members WHERE conversation_id = ? AND character_card_id = ? AND active = 1 LIMIT 1",
                (member["conversation_id"], to_id),
            )
            if keeper_exists:
                await db.execute("DELETE FROM group_members WHERE id = ?", (member["id"],))
                collisions += 1
            else:
                await db.execute("UPDATE group_members SET character_card_id = ? WHERE id = ?", (to_id, member["id"]))

        # Mirror sync_conversations_for_card() inside this transaction.  The
        # reassigned conversations must use the keeper's denormalized fields,
        # not the deleted card's old name and scenario.
        await db.execute(
            "UPDATE conversations SET character_name = ?, character_scenario = ?, post_history_instructions = ? "
            "WHERE character_card_id = ?",
            (keeper["name"], keeper["scenario"], keeper["post_history_instructions"], to_id),
        )
    return {**impact, "collisions": collisions}
