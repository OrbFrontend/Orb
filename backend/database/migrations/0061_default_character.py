"""Migration 0061: give existing installs the shipped default character.

Fresh installs seed the same card and its Dynamic World in ``bootstrap``
(``_seed_default_character``); this is the one-time counterpart for databases
that predate it. Because a migration runs at most once per database, a user who
deletes the character afterwards keeps it deleted.

Idempotent, and it never displaces anything the user already has: a card at the
seed's id (they imported the shipped PNG themselves) means the migration has
nothing to do, and a world already carrying the book's name is linked rather
than duplicated.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from backend.database.seeds import (
    DEFAULT_CHARACTER_COLUMNS,
    DEFAULT_CHARACTER_ID,
    DEFAULT_CHARACTER_WORLD,
    default_character_values,
)


def _has_table(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def migrate(conn: sqlite3.Connection) -> None:
    # Both tables predate the chain, so a real database always has them; a
    # partially-seeded upgrade fixture carries only what its own era created
    # (same reasoning as 0060). Inserting rows is not worth crashing an upgrade
    # over, so a database without somewhere to put them is left alone.
    if not (_has_table(conn, "character_cards") and _has_table(conn, "worlds")):
        return
    if conn.execute("SELECT 1 FROM character_cards WHERE id = ?", (DEFAULT_CHARACTER_ID,)).fetchone():
        return

    now = datetime.now(UTC).isoformat()
    w = DEFAULT_CHARACTER_WORLD

    existing = conn.execute("SELECT id FROM worlds WHERE name = ? LIMIT 1", (w["name"],)).fetchone()
    if existing:
        world_id = existing[0]
    else:
        world_id = w["id"]
        conn.execute(
            "INSERT INTO worlds (id, name, enabled, dynamic_enabled, content_revision, created_at, updated_at)"
            " VALUES (?, ?, 1, ?, 0, ?, ?)",
            (world_id, w["name"], w["dynamic_enabled"], now, now),
        )

    cols = ", ".join(DEFAULT_CHARACTER_COLUMNS)
    placeholders = ", ".join("?" * len(DEFAULT_CHARACTER_COLUMNS))
    conn.execute(
        f"INSERT INTO character_cards ({cols}, created_at, updated_at) VALUES ({placeholders}, ?, ?)",  # nosec B608 — column names from a module constant
        (*default_character_values(world_id), now, now),
    )
    conn.commit()
    print("[migrations] 0061: inserted the default character")
