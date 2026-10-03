"""Backfill absent negated_narration audit toggles with the release default, off.

Preserve explicit choices; reruns are a no-op.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "editor_audit_toggles" not in cols:
        # Column itself is added by 0022; nothing to backfill if it's absent.
        return
    cur = conn.execute(
        "UPDATE settings "
        "SET editor_audit_toggles = json_set(editor_audit_toggles, '$.negated_narration', json('false')) "
        "WHERE json_valid(editor_audit_toggles) "
        "AND json_type(editor_audit_toggles, '$.negated_narration') IS NULL"
    )
    if cur.rowcount:
        print(f"[migrations] 0071: added negated_narration key to {cur.rowcount} settings row(s)")
    conn.commit()
