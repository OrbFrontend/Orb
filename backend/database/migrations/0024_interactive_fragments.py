"""Rename director_fragments to interactive_fragments and use field_type
for feedback routing. Add feedback log/settings fields; reuse Editor timing.
All operations are guarded for reruns.
"""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> None:
    # Rename the old table if needed. If both exist, merge rows then drop the
    # legacy table; historical migrations may have recreated it on a fresh schema.
    if _table_exists(conn, "director_fragments"):
        if not _table_exists(conn, "interactive_fragments"):
            conn.execute("ALTER TABLE director_fragments RENAME TO interactive_fragments")
            conn.commit()
            print("[migrations] 0024: renamed director_fragments -> interactive_fragments")
        else:
            conn.execute(
                """
                INSERT OR IGNORE INTO interactive_fragments
                    (id, label, description, field_type, required, enabled, injection_label, sort_order)
                SELECT id, label, description, field_type, required, enabled, injection_label, sort_order
                FROM director_fragments
                """
            )
            conn.execute("DROP TABLE director_fragments")
            conn.commit()
            print("[migrations] 0024: dropped orphaned director_fragments table")

    if _table_exists(conn, "interactive_fragments"):
        if "target" in _columns(conn, "interactive_fragments"):
            conn.execute("ALTER TABLE interactive_fragments DROP COLUMN target")
            conn.commit()
            print("[migrations] 0024: dropped target column from interactive_fragments")

    log_cols = _columns(conn, "conversation_logs")
    if "feedback" not in log_cols:
        conn.execute("ALTER TABLE conversation_logs ADD COLUMN feedback TEXT NOT NULL DEFAULT '{}'")
        conn.commit()
        print("[migrations] 0024: added feedback column to conversation_logs")

    if "feedback_enabled" not in _columns(conn, "settings"):
        conn.execute("ALTER TABLE settings ADD COLUMN feedback_enabled INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        print("[migrations] 0024: added feedback_enabled column to settings")
