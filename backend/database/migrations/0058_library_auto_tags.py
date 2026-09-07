"""Add the Character Library's curated tag vocabulary.

``table_create_sql`` sources the DDL from ``schema.py`` so an upgraded database
gets a byte-identical shape to a fresh install (the equivalence gate in
``test_migration_chain_completeness``). ``CREATE TABLE IF NOT EXISTS`` makes the
migration idempotent without a ``PRAGMA table_info`` guard.

This originally also created a ``character_auto_tags`` side table holding the
tagger's answers. 0059 folds those into ``character_cards.tags`` and drops it, so
there is nothing left here to create — see that migration for the reasoning.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_tags'").fetchone()
    conn.execute(table_create_sql("library_tags"))
    if not existed:
        print("[migrations] 0058: created library_tags")
