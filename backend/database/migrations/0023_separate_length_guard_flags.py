"""Move length_guard and length_guard_enforce out of enabled_tools into
dedicated flag columns, preserving their values.
"""

from __future__ import annotations

import json
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "length_guard_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN length_guard_enabled INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0023: added length_guard_enabled column to settings")
    if "length_guard_enforce" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN length_guard_enforce INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0023: added length_guard_enforce column to settings")

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

    # Only the two non-tool keys move; everything else is a real tool and stays.
    moved = "length_guard" in tools or "length_guard_enforce" in tools
    enabled = 1 if tools.pop("length_guard", False) else 0
    enforce = 1 if tools.pop("length_guard_enforce", False) else 0

    if moved:
        conn.execute(
            "UPDATE settings SET length_guard_enabled = ?, length_guard_enforce = ?, enabled_tools = ? WHERE id = ?",
            (enabled, enforce, json.dumps(tools), settings_id),
        )
        print(
            f"[migrations] 0023: ported length-guard flags out of enabled_tools "
            f"(length_guard_enabled={enabled}, length_guard_enforce={enforce})"
        )
