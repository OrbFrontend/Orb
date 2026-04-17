"""
0001_editor_rename — rename legacy `refine_assistant_output` → `editor_apply_patch`
in settings.enabled_tools, and `refiner` → `editor` in settings.reasoning_enabled_passes.
"""

from __future__ import annotations

import json
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT id, enabled_tools, reasoning_enabled_passes FROM settings"
    ).fetchone()
    if row is None:
        return

    row_id, raw_tools, raw_passes = row

    # --- enabled_tools: refine_assistant_output → editor_apply_patch ---
    tools: dict = json.loads(raw_tools) if raw_tools else {}
    if "refine_assistant_output" in tools:
        value = tools.pop("refine_assistant_output")
        if "editor_apply_patch" not in tools:
            tools["editor_apply_patch"] = value
        print(
            f"[migrations] 0001: enabled_tools refine_assistant_output={value!r} → "
            f"editor_apply_patch={tools['editor_apply_patch']!r}"
        )

    # --- reasoning_enabled_passes: refiner → editor ---
    passes: dict = json.loads(raw_passes) if raw_passes else {}
    if "refiner" in passes:
        value = passes.pop("refiner")
        if "editor" not in passes:
            passes["editor"] = value
        print(
            f"[migrations] 0001: reasoning_enabled_passes refiner={value!r} → "
            f"editor={passes['editor']!r}"
        )

    conn.execute(
        "UPDATE settings SET enabled_tools = ?, reasoning_enabled_passes = ? WHERE id = ?",
        (json.dumps(tools), json.dumps(passes), row_id),
    )
