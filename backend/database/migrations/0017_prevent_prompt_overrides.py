"""0017_prevent_prompt_overrides -- add prevent_prompt_overrides column to settings.

When enabled, system_prompt and post_history_instructions from character cards are ignored at inference time. Default 0
preserves prior behaviour.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "prevent_prompt_overrides" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN prevent_prompt_overrides INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0017: added prevent_prompt_overrides to settings")
