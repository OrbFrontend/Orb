"""Add the reply_prompts table."""

from __future__ import annotations

import sqlite3

from ..schema import table_create_sql


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(table_create_sql("reply_prompts"))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_reply_prompts_message ON reply_prompts(message_id)")
