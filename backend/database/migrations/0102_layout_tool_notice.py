"""Give the Writer's tool notice its own marker, which rode in front of the Length nudge marker until now.

Each layout gains a Tool notice marker just before its Length nudge marker, so the Writer request keeps its bytes. A rerun
changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout's label; later changes do not rewrite existing rows.
_LABEL = "Tool notice"


def migrate(conn: sqlite3.Connection) -> None:
    layouts = conn.execute(
        "SELECT layout_id, sort_order FROM prompt_blocks b WHERE kind = 'length_nudge'"
        " AND NOT EXISTS (SELECT 1 FROM prompt_blocks t WHERE t.layout_id = b.layout_id AND t.kind = 'tool_notice')"
    ).fetchall()
    for layout_id, at in layouts:
        conn.execute(
            "UPDATE prompt_blocks SET sort_order = sort_order + 1 WHERE layout_id = ? AND sort_order >= ?", (layout_id, at)
        )
        conn.execute(
            "INSERT INTO prompt_blocks (id, layout_id, kind, label, content, region, sort_order) VALUES (?, ?, 'tool_notice', ?, '', 'writer', ?)",
            (f"{layout_id}:tool_notice", layout_id, _LABEL, at),
        )
    conn.commit()
    print("[migrations] 0102: Tool notice blocks added before Length nudge")
