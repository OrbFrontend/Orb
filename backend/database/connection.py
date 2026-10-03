from __future__ import annotations

import json
import os
import sqlite3
from contextlib import asynccontextmanager

import aiosqlite

from ..core.locks import wal_anchor_lock

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db")


def current_db_path() -> str:
    """The live database path, read at call time.

    Code outside this module asks here rather than importing ``DB_PATH``: an
    imported copy is frozen at import, so a test that patches
    ``connection.DB_PATH`` would still reach the real database through it.
    """
    return DB_PATH


# Keep one idle WAL connection open to avoid per-query wal-index teardown.
# The anchor holds no cursor or transaction, allowing VACUUM and online backup.
_wal_anchor: aiosqlite.Connection | None = None
_wal_anchor_path: str | None = None


async def open_wal_anchor() -> None:
    """Open the WAL anchor for DB_PATH, serialized against other opens and closes.

    Idempotent for the same path; close the old anchor before switching paths.
    """
    global _wal_anchor, _wal_anchor_path
    async with wal_anchor_lock():
        path = DB_PATH
        if _wal_anchor is not None:
            if _wal_anchor_path == path:
                return
            await _close_anchor()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        db = await aiosqlite.connect(path)
        try:
            db.row_factory = aiosqlite.Row
            # Each cursor is closed rather than left to the GC: an unfinalized
            # statement would hold a read transaction open on this connection, and
            # the anchor has to stay idle for the maintenance paths above.
            for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA foreign_keys=ON"):
                async with db.execute(pragma) as cur:
                    await cur.fetchall()
        except BaseException:
            # A half-open anchor is worse than none: it would hold the file without
            # the WAL mode the rest of the process assumes.
            await db.close()
            raise
        _wal_anchor = db
        _wal_anchor_path = path


async def close_wal_anchor() -> None:
    """Close the anchor if one is open. A no-op otherwise."""
    async with wal_anchor_lock():
        await _close_anchor()


async def _close_anchor() -> None:
    """Close and forget the anchor. Callers must hold :func:`wal_anchor_lock`.

    Module state is cleared before the close is attempted, so a close that
    raises cannot strand a dead connection object that the next
    ``open_wal_anchor`` would mistake for a live anchor.
    """
    global _wal_anchor, _wal_anchor_path
    db, _wal_anchor, _wal_anchor_path = _wal_anchor, None, None
    if db is not None:
        await db.close()


def checkpoint_wal(db_path: str | None = None, timeout_ms: int = 5000) -> bool:
    """Best-effort WAL checkpoint and truncation after whole-database maintenance.

    The anchor prevents last-close reclamation. Return False when active readers
    block TRUNCATE; auto-checkpoint can reclaim later without affecting correctness.
    """
    path = DB_PATH if db_path is None else db_path
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(timeout_ms)}")
        row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    # (busy, wal_frames, checkpointed_frames); busy=0 means the reset happened.
    return bool(row) and row[0] == 0


@asynccontextmanager
async def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def immediate_tx():
    """Hold BEGIN IMMEDIATE through the body, committing on success.

    Rollback on BaseException, including cancellation, so writers serialize
    before reading and cannot leave partial transactions.
    """
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.execute("ROLLBACK")
            raise


def build_set_clause(
    allowed: list[str], data: dict, json_fields: frozenset[str] | set[str] = frozenset()
) -> tuple[list[str], list]:
    """Build the SET clause lists for a parameterised UPDATE query.

    Returns (sets, vals) where sets is a list of 'col = ?' strings and vals
    holds the corresponding values. Columns in json_fields are JSON-serialised.
    """
    sets: list[str] = []
    vals: list = []
    for k in allowed:
        if k in data:
            sets.append(f"{k} = ?")
            vals.append(json.dumps(data[k]) if k in json_fields else data[k])
    return sets, vals


# Per-workflow JSON slot accessors, shared by the three tables that carry a
# ``workflow_state`` column (conversations, messages, character_cards). The
# read/write pair is identical across them, so only the table and its id column
# vary; both are module-private constants at the call sites and never reach here
# from user input, which is what makes the interpolation below safe (a table
# name cannot be a bound parameter).
async def get_workflow_slot(table: str, id_col: str, row_id, workflow_id: str) -> dict | None:
    """Return the workflow's slot on this row, or None if the row is missing or the slot empty."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                f"SELECT json_extract(workflow_state, '$.' || ?) AS slot FROM {table} WHERE {id_col} = ?",
                (workflow_id, row_id),
            )
        )
        if not rows:
            return None
        slot = rows[0]["slot"]
        if slot is None:
            return None
        return json.loads(slot)


async def set_workflow_slot(table: str, id_col: str, row_id, workflow_id: str, payload: dict | None) -> None:
    """Atomic per-slot write via SQLite JSON1.

    payload=None removes the slot. Empty dict stores {}. No-op if the row is
    missing (UPDATE matches zero rows).
    """
    async with get_db() as db:
        if payload is None:
            await db.execute(
                f"UPDATE {table} "  # nosec B608 -- table and id_col are code constants
                "SET workflow_state = json_remove(COALESCE(workflow_state, '{}'), '$.' || ?) "
                f"WHERE {id_col} = ?",
                (workflow_id, row_id),
            )
        else:
            await db.execute(
                f"UPDATE {table} "  # nosec B608 -- table and id_col are code constants
                "SET workflow_state = json_set(COALESCE(workflow_state, '{}'), '$.' || ?, json(?)) "
                f"WHERE {id_col} = ?",
                (workflow_id, json.dumps(payload), row_id),
            )
        await db.commit()
