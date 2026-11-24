"""Give each layout's lore markers their own text, the framing each was sent with. A rerun changes nothing."""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout's templates; later changes do not rewrite existing rows.
_TEMPLATES = {
    "constant_lore": "## Lorebook\n\n{{constant_lore}}",
    "selected_lore": "___\n\n**Lorebook**\n\n{{selected_lore}}",
    "depth_lore": "___\n\n**Lorebook (Depth)**\n\n{{depth_lore}}",
}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "UPDATE prompt_blocks SET content = ? WHERE kind = ? AND content = ''",
        [(text, kind) for kind, text in _TEMPLATES.items()],
    )
    conn.commit()
    print("[migrations] 0101: lore blocks hold their text")
