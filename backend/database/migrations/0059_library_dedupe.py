"""Add the Character Library duplicate finder's dismissals table and avatar-hash cache.

Two pieces, both mirroring 0058 exactly:

* ``duplicate_dismissals`` -- the pairs the user has told the scan to stop
  reporting, stamped with both cards' ``body_hash`` so a dismissal lapses only
  when a card meaningfully changes. ``table_create_sql`` sources the DDL from
  ``schema.py`` so an upgraded database gets the same shape as a fresh install
  (the chain-completeness gate in ``test_migration_chain_completeness``), and
  ``CREATE TABLE IF NOT EXISTS`` keeps it idempotent without a ``PRAGMA`` guard.
* ``character_cards.avatar_dhash`` / ``avatar_dhash_stamp`` -- the scan's only
  cached signal. Decoding avatars is ~70x the cost of every text signal
  combined, so the hash is stored and the text side is recomputed each run.
  Staleness is ``avatar_dhash_stamp != f"{DEDUPE_REVISION}:{updated_at}"``, the
  direct analogue of ``_PENDING_WHERE`` in ``queries/library_tags.py``.
"""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def migrate(conn: sqlite3.Connection) -> None:
    existed = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='duplicate_dismissals'").fetchone()
    conn.execute(table_create_sql("duplicate_dismissals"))
    if not existed:
        print("[migrations] 0059: created duplicate_dismissals")

    card_cols = _columns(conn, "character_cards")
    if not card_cols:
        return
    for column in ("avatar_dhash", "avatar_dhash_stamp"):
        if column not in card_cols:
            conn.execute(f"ALTER TABLE character_cards ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")  # nosec B608
            print(f"[migrations] 0059: added {column} column to character_cards")
