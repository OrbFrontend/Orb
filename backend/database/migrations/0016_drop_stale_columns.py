"""Drop obsolete swipe_index, is_active and conversation first_mes columns.

Branching uses the message tree; first_mes is already stored as a message.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    message_cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "swipe_index" in message_cols:
        conn.execute("ALTER TABLE messages DROP COLUMN swipe_index")
        conn.commit()
        print("[migrations] 0016: dropped messages.swipe_index")

    if "is_active" in message_cols:
        conn.execute("ALTER TABLE messages DROP COLUMN is_active")
        conn.commit()
        print("[migrations] 0016: dropped messages.is_active")

    conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "first_mes" in conv_cols:
        conn.execute("ALTER TABLE conversations DROP COLUMN first_mes")
        conn.commit()
        print("[migrations] 0016: dropped conversations.first_mes")
