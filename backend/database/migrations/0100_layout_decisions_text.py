"""Give each layout's Decisions marker its own text, the framing it was sent with. A rerun changes nothing."""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout's template; later changes do not rewrite existing rows.
_TEMPLATE = "___\n\n**Major Decisions**\n\n{{decisions}}"


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE prompt_blocks SET content = ? WHERE kind = 'decisions' AND content = ''", (_TEMPLATE,))
    conn.commit()
    print("[migrations] 0100: Decisions blocks hold their text")
