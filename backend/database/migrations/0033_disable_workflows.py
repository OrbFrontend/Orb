"""Add global and per-workflow enablement, defaulting to on.

Move an explicit format_consistency config disable into workflow_enabled
and remove the old key; its absence makes reruns a no-op.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    add_columns(
        conn,
        "settings",
        "workflows_globally_enabled INTEGER NOT NULL DEFAULT 1",
        "workflow_enabled TEXT NOT NULL DEFAULT '{}'",
        migration="0033",
    )

    # Carry a prior format_consistency disable from the retired config flag into the framework toggle. json_extract returns 0
    # for a stored `false`, 1 for `true`, and NULL when the key is absent (the only case on a fresh DB).
    row = conn.execute(
        "SELECT json_extract(workflow_config, '$.format_consistency.enabled') FROM settings WHERE id = 1"
    ).fetchone()
    if row is not None and row[0] == 0:
        conn.execute(
            "UPDATE settings "
            "SET workflow_enabled = json_set(COALESCE(workflow_enabled, '{}'), '$.format_consistency', json('false')) "
            "WHERE id = 1"
        )
        conn.execute(
            "UPDATE settings SET workflow_config = json_remove(workflow_config, '$.format_consistency.enabled') WHERE id = 1"
        )
        print("[migrations] 0033: carried prior format_consistency disable into workflow_enabled")
