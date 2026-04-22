    """
0009_shared_system_prompt — add shared_system_prompt column to settings,
copy existing system_prompt to it, and reset model-specific system_prompts.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    # Add shared_system_prompt column to settings if not exists
    settings_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()
    }
    if "shared_system_prompt" not in settings_cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN shared_system_prompt TEXT NOT NULL DEFAULT ''"
        )

    print("[migrations] 0009: shared_system_prompt added")
