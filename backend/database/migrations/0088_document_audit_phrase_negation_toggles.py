"""Backfill absent phrase_repetition (on) and negated_narration (off) document audit toggles.

Preserve explicit choices; reruns are a no-op.
"""

from __future__ import annotations

import sqlite3

_DEFAULTS = {"phrase_repetition": "true", "negated_narration": "false"}


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "document_audit_toggles" not in cols:
        return
    for key, value in _DEFAULTS.items():
        conn.execute(
            f"UPDATE settings SET document_audit_toggles = json_set(document_audit_toggles, '$.{key}', json('{value}')) "  # nosec B608
            f"WHERE json_valid(document_audit_toggles) AND json_type(document_audit_toggles, '$.{key}') IS NULL"
        )
    conn.commit()
