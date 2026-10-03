"""Backfill the enabled anti_echo audit toggle to match the runtime default."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "editor_audit_toggles" not in cols:
        # Column itself is added by 0022; nothing to backfill if it's absent.
        return
    cur = conn.execute(
        "UPDATE settings "
        "SET editor_audit_toggles = json_set(editor_audit_toggles, '$.anti_echo', json('true')) "
        "WHERE json_extract(editor_audit_toggles, '$.anti_echo') IS NULL"
    )
    if cur.rowcount:
        print(f"[migrations] 0031: added anti_echo key to {cur.rowcount} settings row(s)")
