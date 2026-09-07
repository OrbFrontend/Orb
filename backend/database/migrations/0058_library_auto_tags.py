"""Add the Character Library's curated tag vocabulary and its auto-tag store.

Both tables belong to the auto-tagger and nothing else reads them, so they are
created here rather than bolted onto ``character_cards``: an imported card's own
``tags`` column is never touched by a tagging run, and removing the feature is a
matter of dropping these two tables.

``table_create_sql`` sources the DDL from ``schema.py`` so an upgraded database
gets byte-identical shapes to a fresh install (the equivalence gate in
``test_migration_chain_completeness``). ``CREATE TABLE IF NOT EXISTS`` makes the
whole migration idempotent without a ``PRAGMA table_info`` guard.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    for table in ("library_tags", "character_auto_tags"):
        existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        conn.execute(table_create_sql(table))
        if not existed:
            print(f"[migrations] 0058: created {table}")
