"""0056_message_writer_draft -- retain an original Writer source for each reply.

The local Prose Rewriter can be invoked after a turn completes. Its correct
source is the Writer output (with inline macros already frozen) before the
local rewriter, Editor, or post-turn workflows changed the visible message, so
new assistant rows retain that text in ``writer_draft``. Existing rows
intentionally remain NULL: the old source was never stored and guessing from
edited content would be misleading.
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
