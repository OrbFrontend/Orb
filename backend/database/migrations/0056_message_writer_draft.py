"""0056_message_writer_draft -- retain a prose-rewrite source for each reply.

The local Prose Rewriter can be invoked after a turn completes. The column name
reflects its original capture point; the current pipeline retains post-Editor
text before the local rewriter or later workflows change the visible message.
Existing rows intentionally remain NULL when no source was stored. Populated
rows from an older release may still contain the earlier pre-Editor source.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if not cols:
        # Fresh installs create the full shape from schema.py before migrations
        # are stamped. A malformed legacy DB without messages should not block
        # the rest of startup here.
        return
    if "writer_draft" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN writer_draft TEXT DEFAULT NULL")
        print("[migrations] 0056: added messages.writer_draft")
