"""Migration 0029: lifetime generated-chars counter for homepage stats.

Adds ``settings.generated_chars``, the running total of characters the LLM has
generated (the homepage "~Tokens generated" stat divides it by the
CHARS_PER_TOKEN heuristic). NULL means "not yet seeded": the stats query layer
lazily initialises it from the existing assistant-message rows on first use,
then successful turns increment it -- so no backfill happens here, and a
restored backup without the column self-heals the same way.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "generated_chars" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN generated_chars INTEGER DEFAULT NULL")
        conn.commit()
        print("[migrations] 0029: added generated_chars column to settings")
