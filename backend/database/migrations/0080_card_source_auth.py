"""Add per-source card-site logins as JSON, defaulting to {}.

Each entry holds a site's session token and the account name it belongs to, never a password.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if not cols:
        # No settings table yet: init_db creates it from schema.py with this column already on it.
        return
    add_columns(conn, "settings", "card_source_auth TEXT NOT NULL DEFAULT '{}'", migration="0080")
