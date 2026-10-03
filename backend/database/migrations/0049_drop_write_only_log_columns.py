"""Drop write-only agent_raw_output and progressive_fields_after log columns.

Raw output remains in application logs; branch state comes from the message tree.
"""

from __future__ import annotations

import sqlite3

_WRITE_ONLY_COLUMNS = ("agent_raw_output", "progressive_fields_after")


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_logs)").fetchall()}
    for col in _WRITE_ONLY_COLUMNS:
        if col in cols:
            conn.execute(f"ALTER TABLE conversation_logs DROP COLUMN {col}")
            print(f"[migrations] 0049: dropped write-only conversation_logs.{col}")
