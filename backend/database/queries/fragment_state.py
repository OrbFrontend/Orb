"""State-fragment event storage: path reads, anchored writes, copies, and snapshots."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

import aiosqlite

from ...core import StateView, fold_events
from ..connection import get_db
from ..models import FragmentStateEventRow

_SQL_PARAM_CHUNK = 900

_INSERT = (
    "INSERT INTO fragment_state_events "
    "(conversation_id, message_id, fragment_id, entry_id, op, text, mode, fragment_label, source, created_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _row_values(cid: str, message_id: int, event: Mapping[str, Any], now: str) -> tuple:
    return (
        cid,
        message_id,
        event["fragment_id"],
        event["entry_id"],
        event["op"],
        event.get("text"),
        event.get("mode"),
        event.get("fragment_label") or "",
        event.get("source") or "agent",
        event.get("created_at") or now,
    )


async def insert_state_events(
    db: aiosqlite.Connection, cid: str, message_id: int, events: Sequence[Mapping[str, Any]], now: str
) -> None:
    """Insert *events* in order on an open connection, inside the caller's transaction.

    ``add_message`` uses this so a reply and the state it produced commit together.
    """
    if events:
        await db.executemany(_INSERT, [_row_values(cid, message_id, event, now) for event in events])


async def add_state_events(cid: str, message_id: int, events: Sequence[Mapping[str, Any]]) -> None:
    """Commit *events* anchored on *message_id* in one transaction."""
    if not events:
        return
    now = datetime.now(UTC).isoformat()
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await insert_state_events(db, cid, message_id, events, now)
        await db.commit()


async def get_state_events_for_path(cid: str, path_message_ids: Sequence[int]) -> list[FragmentStateEventRow]:
    """Return path events in path order, then insertion order within each message."""
    if not path_message_ids:
        return []
    rank = {mid: i for i, mid in enumerate(path_message_ids)}
    rows: list[Any] = []
    ids = list(path_message_ids)
    async with get_db() as db:
        for start in range(0, len(ids), _SQL_PARAM_CHUNK):
            chunk = ids[start : start + _SQL_PARAM_CHUNK]
            placeholders = ",".join("?" for _ in chunk)
            rows.extend(
                await db.execute_fetchall(
                    f"SELECT * FROM fragment_state_events WHERE conversation_id = ? AND message_id IN ({placeholders})",  # nosec B608 -- placeholders are a fixed-count '?' list
                    (cid, *chunk),
                )
            )
    out = [cast(FragmentStateEventRow, dict(r)) for r in rows]
    out.sort(key=lambda r: (rank[r["message_id"]], r["id"]))
    return out


async def get_state_events_for_message(
    message_id: int, *, sources: Collection[str] | None = None
) -> list[FragmentStateEventRow]:
    """One message's own events in row order, optionally limited to *sources*."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall("SELECT * FROM fragment_state_events WHERE message_id = ? ORDER BY id", (message_id,))
        )
    out = [cast(FragmentStateEventRow, dict(r)) for r in rows]
    return [r for r in out if sources is None or r["source"] in sources]


async def fold_path_state(cid: str, path_message_ids: Sequence[int]) -> StateView:
    """The active entries after folding every event on *path_message_ids*."""
    return fold_events(await get_state_events_for_path(cid, path_message_ids))


async def copy_state_events(source_cid: str, target_cid: str, id_map: Mapping[int, int]) -> int:
    """Copy events through a message-id map, preserving their history and order."""
    events = await get_state_events_for_path(source_cid, list(id_map))
    if not events:
        return 0
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        await db.executemany(
            _INSERT,
            [_row_values(target_cid, id_map[event["message_id"]], event, event["created_at"]) for event in events],
        )
        await db.commit()
    return len(events)


async def snapshot_state_to_message(cid: str, view: StateView, message_id: int) -> int:
    """Write active entries as ``carried`` events on a compressed summary."""
    events = [
        {
            "fragment_id": fid,
            "entry_id": entry.entry_id,
            "op": "add",
            "text": entry.text,
            "fragment_label": view.labels.get(fid, fid),
            "source": "carried",
        }
        for fid, active in view.entries.items()
        for entry in active.values()
    ]
    await add_state_events(cid, message_id, events)
    return len(events)


async def delete_fragment_state(cid: str, fragment_id: str) -> int:
    """Delete one fragment's whole saved history in a conversation, every branch."""
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM fragment_state_events WHERE conversation_id = ? AND fragment_id = ?", (cid, fragment_id)
        )
        await db.commit()
        return cur.rowcount
