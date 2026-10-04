"""Move length_guard and length_guard_enforce out of enabled_tools into dedicated flag columns, preserving their values."""

from __future__ import annotations

import json
import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    add_columns(
        conn,
        "settings",
        "length_guard_enabled INTEGER NOT NULL DEFAULT 0",
        "length_guard_enforce INTEGER NOT NULL DEFAULT 0",
        migration="0023",
    )

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
