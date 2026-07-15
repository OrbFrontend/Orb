"""
0042_drop_prompt_rewriter -- remove the Prompt Rewriter feature's settings key.

The rewrite_user_prompt director tool was removed from the codebase; strip its
entry from the enabled_tools JSON so stored databases (and imported presets,
which replay migrations) carry no trace of the retired feature.
"""

from __future__ import annotations

import json
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT id, enabled_tools FROM settings").fetchone()
    if row is None:
        return

    settings_id, raw = row[0], row[1]
    try:
        tools = json.loads(raw or "{}")
    except (TypeError, ValueError):
        tools = {}
    if not isinstance(tools, dict):
        tools = {}

    if "rewrite_user_prompt" in tools:
        tools.pop("rewrite_user_prompt")
        conn.execute(
            "UPDATE settings SET enabled_tools = ? WHERE id = ?",
            (json.dumps(tools), settings_id),
        )
        print("[migrations] 0042: stripped rewrite_user_prompt from enabled_tools")
