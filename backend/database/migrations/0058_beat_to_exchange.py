"""0058_beat_to_exchange -- rename the group turn unit from `beat` to `exchange`.

Vocabulary only: the column holds exactly what it held before, one id shared by
every reply a single group request produced. `beat` was borrowed from
screenwriting for a unit that is not a story beat at all, and the word was
already spoken for -- `pipeline/cast.py` uses it for the Director's *per-speaker*
one-line note, which is a story beat and keeps the name.

Both renames handle the **re-run path**, the same way ``0006`` did: a fresh
install builds `messages` from today's ``schema.py``, so it already has
``exchange_id`` before ``0054`` runs and re-adds ``beat_id`` beside it. Nothing
was ever written to that stray column on such a database, so it is dropped
rather than merged.

The index is dropped *before* the column moves and rebuilt after, rather than
left to SQLite's rename rewrite: an index cannot be left pointing at a dropped
column, and ``idx_messages_beat`` over a column called ``exchange_id`` is the
half-state this migration exists to avoid.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 — literal table name


def _rename(conn: sqlite3.Connection, table: str) -> None:
    columns = _columns(conn, table)
    if "beat_id" not in columns:
        return
    if "exchange_id" in columns:
        conn.execute(f"ALTER TABLE {table} DROP COLUMN beat_id")  # nosec B608 — literal table name
        print(f"[migrations] 0058: dropped the re-added {table}.beat_id")
        return
    conn.execute(f"ALTER TABLE {table} RENAME COLUMN beat_id TO exchange_id")  # nosec B608 — literal table name
    print(f"[migrations] 0058: renamed {table}.beat_id to exchange_id")


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_messages_beat")
    _rename(conn, "messages")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_exchange ON messages(conversation_id, exchange_id)")
    _rename(conn, "member_sheet_proposals")
