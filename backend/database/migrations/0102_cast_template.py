"""Give each layout's Group cast block its member card text: the card a group renders for each member, now editable.

Only blocks still holding no text take it. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout.CAST_TEMPLATE; later changes do not rewrite existing rows.
_CAST_TEMPLATE = "## Character: {{char}}\n{{description}}\n\n{{personality}}\n\n## Example Dialogue\n{{examples}}"


def migrate(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'prompt_blocks'").fetchone():
        return
    filled = conn.execute("UPDATE prompt_blocks SET content = ? WHERE kind = 'cast' AND content = ''", (_CAST_TEMPLATE,))
    conn.commit()
    print(f"[migrations] 0102: {filled.rowcount} Group cast blocks given the member card text")
