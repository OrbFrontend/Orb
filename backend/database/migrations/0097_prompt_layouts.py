"""Add prompt layouts: ordered prompt blocks around the chat history, and the active layout setting.

Seeds the 'default' layout with every marker and its template. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout's markers, labels and templates; later changes do not rewrite existing rows.
_DIVIDED = "___\n\n{{content}}"
_MARKERS = (
    ("main_prompt", "Main prompt", "prefix", "{{content}}"),
    ("character", "Character", "prefix", "## Character: {{char}}\n{{content}}"),
    ("cast", "Group cast", "prefix", "{{content}}"),
    ("constant_lore", "Constant lore", "prefix", "{{content}}"),
    ("scenario", "Scenario", "prefix", "## Scenario\n{{content}}"),
    ("example_dialogue", "Example dialogue", "prefix", "## Example Dialogue\n{{content}}"),
    ("card_instructions", "Chat instructions", "prefix", "## Additional Instructions\n{{content}}"),
    ("user_persona", "User persona", "prefix", "## User: {{user}}\n{{content}}"),
    ("workflow_blocks", "Workflow blocks", "prefix", "{{content}}"),
    ("selected_lore", "Selected lore", "writer", _DIVIDED),
    ("scene_guidance", "Scene guidance", "writer", _DIVIDED),
    ("length_nudge", "Length nudge", "writer", "{{content}}"),
    ("cue", "Speaker cue", "writer", "## Your cue\n{{content}}"),
    ("user_message", "User message", "writer", _DIVIDED),
    ("depth_lore", "Depth lore", "writer", _DIVIDED),
)


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS prompt_layouts (id TEXT PRIMARY KEY, name TEXT NOT NULL)")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS prompt_blocks (
            id TEXT PRIMARY KEY,
            layout_id TEXT NOT NULL REFERENCES prompt_layouts(id) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            label TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            role TEXT NOT NULL DEFAULT 'system',
            region TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_prompt_blocks_layout ON prompt_blocks(layout_id, sort_order)")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if columns and "active_prompt_layout_id" not in columns:
        conn.execute("ALTER TABLE settings ADD COLUMN active_prompt_layout_id TEXT DEFAULT NULL")
    if conn.execute("SELECT COUNT(*) FROM prompt_layouts").fetchone()[0] == 0:
        conn.execute("INSERT INTO prompt_layouts (id, name) VALUES ('default', 'Default')")
        conn.executemany(
            "INSERT INTO prompt_blocks (id, layout_id, kind, label, content, region, sort_order)"
            " VALUES (?, 'default', ?, ?, ?, ?, ?)",
            [
                (f"default:{kind}", kind, label, content, region, index)
                for index, (kind, label, region, content) in enumerate(_MARKERS)
            ],
        )
    conn.commit()
    print("[migrations] 0097: prompt layouts added")
