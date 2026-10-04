"""0009_shared_system_prompt — add shared_system_prompt column to settings, copy existing system_prompt to it, and reset
model-specific system_prompts.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    # Add shared_system_prompt column to settings if not exists
    add_columns(conn, "settings", "shared_system_prompt TEXT NOT NULL DEFAULT ''")

    print("[migrations] 0009: shared_system_prompt added")
