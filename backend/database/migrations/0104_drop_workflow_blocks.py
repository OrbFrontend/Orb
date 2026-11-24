"""Drop the Workflow blocks marker; no workflow adds prompt text any more. A rerun changes nothing."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM prompt_blocks WHERE kind = 'workflow_blocks'")
    conn.commit()
    print("[migrations] 0104: Workflow blocks marker dropped")
