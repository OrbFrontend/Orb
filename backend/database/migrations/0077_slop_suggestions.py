"""Add the Phrase Bank suggestion miner's tables.

``slop_suggestions`` holds the current run's suggestions, ``slop_dismissals``
the keys the user dismissed, and ``slop_mining_state`` the staleness
bookkeeping. ``table_create_sql`` sources the DDL from ``schema.py`` so an
upgraded database matches a fresh install, and ``CREATE TABLE IF NOT EXISTS``
keeps the migration idempotent.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    for table in ("slop_suggestions", "slop_dismissals", "slop_mining_state"):
        conn.execute(table_create_sql(table))
