from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

import aiosqlite

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "app.db")


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
    """A connection with SQLite's write lock already held, committed or rolled back.

    ``BEGIN IMMEDIATE`` up front, so two writers serialise here instead of
    interleaving reads and only discovering the conflict at commit time. The
    body commits on a clean exit and rolls back on any exception --
    ``BaseException``, so a cancelled request cannot leave a half-applied
    transaction behind either.
    """
    async with get_db() as db:
        await db.execute("BEGIN IMMEDIATE")
        try:
            yield db
            await db.commit()
        except BaseException:
            await db.execute("ROLLBACK")
            raise


def _build_set_clause(
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
async def _get_workflow_slot(table: str, id_col: str, row_id, workflow_id: str) -> dict | None:
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


async def _set_workflow_slot(table: str, id_col: str, row_id, workflow_id: str, payload: dict | None) -> None:
    """Atomic per-slot write via SQLite JSON1.

    payload=None removes the slot. Empty dict stores {}. No-op if the row is
    missing (UPDATE matches zero rows).
    """
    async with get_db() as db:
        if payload is None:
            await db.execute(
                f"UPDATE {table} "
                "SET workflow_state = json_remove(COALESCE(workflow_state, '{}'), '$.' || ?) "
                f"WHERE {id_col} = ?",
                (workflow_id, row_id),
            )
        else:
            await db.execute(
                f"UPDATE {table} "
                "SET workflow_state = json_set(COALESCE(workflow_state, '{}'), '$.' || ?, json(?)) "
                f"WHERE {id_col} = ?",
                (workflow_id, json.dumps(payload), row_id),
            )
        await db.commit()
