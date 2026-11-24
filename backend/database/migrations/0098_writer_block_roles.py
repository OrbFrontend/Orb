"""Give every block below the chat history the user role.

Roles now apply below the history too. Until now every block there rendered into the Writer's one user message whatever its
stored role, so setting them all to ``user`` keeps each layout's Writer request as it was.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'prompt_blocks'").fetchone():
        return
    changed = conn.execute("UPDATE prompt_blocks SET role = 'user' WHERE region = 'writer' AND role != 'user'")
    conn.commit()
    print(f"[migrations] 0098: {changed.rowcount} blocks below the chat history set to the user role")
