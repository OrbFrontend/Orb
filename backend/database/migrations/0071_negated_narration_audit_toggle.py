"""
0071_negated_narration_audit_toggle -- add the `negated_narration` key to
existing rows' editor_audit_toggles, persisting the scanner's release default
(off). New databases already get the key from the column default (schema.py).

Only an absent key is backfilled: an explicit true or false choice survives,
and a rerun changes nothing. run_audit's `_on()` also reads a missing key as
this default, so the migration keeps the persisted JSON (and the settings UI
checkbox) consistent rather than establishing the default by itself.
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
