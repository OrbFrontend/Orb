"""Store the active preset id that a NULL or stale settings.active_preset_id resolved to.

0097 added the active layout setting as NULL and only a layout switch filled it, so most databases reached 0101 with NULL, which
read as the first preset. The frontend reads the column itself, so it now names that preset. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if not {"settings", "presets"} <= tables:
        return
    # The order get_active_preset_id falls back on: 'default' first, then by name.
    filled = conn.execute(
        """UPDATE settings SET active_preset_id = (
               SELECT id FROM presets ORDER BY id != 'default', name COLLATE NOCASE LIMIT 1
           )
           WHERE id = 1 AND EXISTS (SELECT 1 FROM presets)
             AND (active_preset_id IS NULL OR active_preset_id NOT IN (SELECT id FROM presets))"""
    )
    conn.commit()
    print(f"[migrations] 0102: active preset {'stored' if filled.rowcount else 'already set'}")
