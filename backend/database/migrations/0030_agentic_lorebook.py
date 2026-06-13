"""
0030_agentic_lorebook -- add the `agentic_lorebook_enabled` feature-flag column.

When set, the Director (the direct_scene pass) chooses which lorebook entries
are relevant each turn from a compact catalog, bypassing the keyword scan. It is
a feature flag, not a model-callable tool, so it lives in its own settings
column rather than in enabled_tools (mirrors length_guard_enabled).
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "agentic_lorebook_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN agentic_lorebook_enabled INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0030: added agentic_lorebook_enabled column to settings")
