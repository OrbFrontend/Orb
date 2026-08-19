"""0056_group_sheet_override -- a scene-local sheet a member can read about itself.

``group_members`` already carried ``public_profile_override``: what the *rest*
of the cast sees. It had no counterpart for what the member reads about
*itself*, so a speaker's own description and personality were pinned to card
text for the life of the scene — still asserting turn one after the transcript
had cut its hair, burned its coat and broken its sword.

``card_sheet_override`` is that counterpart. NULL means "fall back to the card",
which is what every existing row means and why nothing is backfilled here. The
card itself is never written: a card stays a reusable shared asset, and the
original is always recoverable by clearing the override.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 — literal table name


def migrate(conn: sqlite3.Connection) -> None:
    if "card_sheet_override" not in _columns(conn, "group_members"):
        conn.execute("ALTER TABLE group_members ADD COLUMN card_sheet_override TEXT DEFAULT NULL")
        print("[migrations] 0056: added card_sheet_override column to group_members")
