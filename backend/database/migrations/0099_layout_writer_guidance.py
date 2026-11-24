"""Split the Writer's Director-stage block and give Scene guidance and User message their own text.

Each layout gains a Decisions marker just before its Scene guidance marker and a Current state marker just after it, the order
the three parts rode in as one block. Scene guidance and User message rows still empty take their starting text. A rerun
changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout's templates and labels; later changes do not rewrite existing rows.
_TEMPLATES = {
    "scene_guidance": "___\n\n**Scene Guidance**\n\n{{scene_guidance}}",
    "user_message": "___\n\n{{user_message}}",
}
_LABELS = {"decisions": "Decisions", "current_state": "Current state"}


def migrate(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "UPDATE prompt_blocks SET content = ? WHERE kind = ? AND content = ''",
        [(text, kind) for kind, text in _TEMPLATES.items()],
    )
    layouts = conn.execute(
        "SELECT layout_id, sort_order FROM prompt_blocks b WHERE kind = 'scene_guidance'"
        " AND NOT EXISTS (SELECT 1 FROM prompt_blocks d WHERE d.layout_id = b.layout_id AND d.kind = 'decisions')"
    ).fetchall()
    for layout_id, at in layouts:
        conn.execute(
            "UPDATE prompt_blocks SET sort_order = sort_order + 2 WHERE layout_id = ? AND sort_order > ?", (layout_id, at)
        )
        conn.execute(
            "UPDATE prompt_blocks SET sort_order = ? WHERE layout_id = ? AND kind = 'scene_guidance'", (at + 1, layout_id)
        )
        conn.executemany(
            "INSERT INTO prompt_blocks (id, layout_id, kind, label, content, region, sort_order) VALUES (?, ?, ?, ?, '', 'writer', ?)",
            [
                (f"{layout_id}:decisions", layout_id, "decisions", _LABELS["decisions"], at),
                (f"{layout_id}:current_state", layout_id, "current_state", _LABELS["current_state"], at + 2),
            ],
        )
    conn.commit()
    print("[migrations] 0099: Decisions and Current state blocks added; Scene guidance and User message hold their text")
