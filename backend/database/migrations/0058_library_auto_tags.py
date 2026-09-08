"""Add the Character Library's curated tag vocabulary and the auto-tagger stamps.

The feature landed over four migrations (0058-0061) that argued with each other:
a ``character_auto_tags`` side table, then folding it into ``character_cards``,
then an ``imported_tags`` stash column, then dropping the stash. None of that
shipped anywhere, so the chain is collapsed to the shape it settled on:

* ``library_tags`` -- the ordered vocabulary the library filter draws its chips
  from. ``table_create_sql`` sources the DDL from ``schema.py`` so an upgraded
  database gets the same shape as a fresh install (the chain-completeness gate
  in ``test_migration_chain_completeness``). ``CREATE TABLE IF NOT EXISTS`` keeps
  it idempotent without a ``PRAGMA`` guard.
* ``character_cards.auto_tag_vocab_hash`` / ``auto_tag_card_updated_at`` -- the
  staleness rule: a card is re-tagged when the vocabulary or the card itself has
  changed since its last run. A run rewrites the card's own ``tags`` column, so
  there is one visible tag list, not two.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def migrate(conn: sqlite3.Connection) -> None:
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='library_tags'").fetchone()
    conn.execute(table_create_sql("library_tags"))
    if not existed:
        print("[migrations] 0058: created library_tags")

    card_cols = _columns(conn, "character_cards")
    if not card_cols:
        return
    for column in ("auto_tag_vocab_hash", "auto_tag_card_updated_at"):
        if column not in card_cols:
            conn.execute(f"ALTER TABLE character_cards ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")  # nosec B608
            print(f"[migrations] 0058: added {column} column to character_cards")
