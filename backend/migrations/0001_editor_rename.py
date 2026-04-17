"""
0001_editor_rename — rename the old `refine_assistant_output` tool key to
`editor_apply_patch` in the settings.enabled_tools JSON column.
"""

from __future__ import annotations

import json
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT id, enabled_tools FROM settings").fetchone()
    if row is None:
        return

    row_id, raw = row
    tools: dict = json.loads(raw) if raw else {}

    if "refine_assistant_output" not in tools:
        return

    value = tools.pop("refine_assistant_output")
    if "editor_apply_patch" not in tools:
        tools["editor_apply_patch"] = value

    conn.execute(
        "UPDATE settings SET enabled_tools = ? WHERE id = ?",
        (json.dumps(tools), row_id),
    )
    print(
        f"[migrations] 0001: refine_assistant_output={value!r} → "
        f"editor_apply_patch={tools['editor_apply_patch']!r}"
    )
