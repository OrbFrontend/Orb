"""Migration 0024: Interactive Fragments + Editor Feedback.

Renames ``director_fragments`` -> ``interactive_fragments`` and folds the
feedback feature onto a fragment *type* rather than a routing column:

* Rename the table (if a pre-rename DB still has it).
* Drop the ``target`` column if an earlier build of this (unreleased) feature
  added it -- feedback fragments are now identified by ``field_type='feedback'``.
* Add the ``feedback`` column to ``conversation_logs`` (the feedback sub-step's
  user-facing note) and the ``feedback_enabled`` setting. (The feedback step is
  an editor sub-step: it shares the editor's reasoning/latency, so it gets no
  reasoning_feedback / feedback_latency_ms columns of its own.)

Everything is guarded so it is a no-op on fresh installs (schema.py already
builds the post-rename shape) and idempotent across reruns.
"""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> None:
    # Rename the table only when the old one exists and the new one does not, so
    # this is safe whether the DB predates the feature or was created fresh.
    if _table_exists(conn, "director_fragments") and not _table_exists(conn, "interactive_fragments"):
        conn.execute("ALTER TABLE director_fragments RENAME TO interactive_fragments")
        conn.commit()
        print("[migrations] 0024: renamed director_fragments -> interactive_fragments")

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
