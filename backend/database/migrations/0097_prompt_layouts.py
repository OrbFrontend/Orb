"""Add prompt layouts: ordered prompt blocks around the chat history, and the active layout setting.

Seeds the 'default' layout, its Main prompt holding the global system prompt, then drops the system prompt settings and the model
configs' system prompts. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout's default layout: (id suffix, kind, label, region, text); later changes do not rewrite
# existing rows. The Main prompt's text comes from the settings row.
_BLOCKS = (
    ("main_prompt", "main_prompt", "Main prompt", "prefix", ""),
    ("character", "custom", "Character", "prefix", "## Character: {{char}}\n{{description}}\n\n{{personality}}"),
    ("cast", "cast", "Group cast", "prefix", ""),
    ("constant_lore", "constant_lore", "Constant lore", "prefix", "## Lorebook\n\n{{constant_lore}}"),
    ("scenario", "custom", "Scenario", "prefix", "## Scenario\n{{scenario}}"),
    ("example_dialogue", "custom", "Example dialogue", "prefix", "## Example Dialogue\n{{examples}}"),
    ("card_instructions", "custom", "Chat instructions", "prefix", "## Additional Instructions\n{{instructions}}"),
    ("user_persona", "custom", "User persona", "prefix", "## User: {{user}}\n{{persona}}"),
    ("selected_lore", "selected_lore", "Selected lore", "writer", "___\n\n**Lorebook**\n\n{{selected_lore}}"),
    ("decisions", "decisions", "Decisions", "writer", "___\n\n**Major Decisions**\n\n{{decisions}}"),
    ("scene_guidance", "scene_guidance", "Scene guidance", "writer", "___\n\n**Scene Guidance**\n\n{{scene_guidance}}"),
    ("user_message", "user_message", "User message", "writer", "___\n\n{{user_message}}"),
    ("depth_lore", "depth_lore", "Depth lore", "writer", "___\n\n**Lorebook (Depth)**\n\n{{depth_lore}}"),
)
_DROPPED = (
    ("settings", "shared_system_prompt"),
    ("settings", "system_prompt"),
    ("settings", "agent_shared_system_prompt"),
    ("model_configs", "system_prompt"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- fixed table names


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
    settings = _columns(conn, "settings")
    if settings and "active_prompt_layout_id" not in settings:
        conn.execute("ALTER TABLE settings ADD COLUMN active_prompt_layout_id TEXT DEFAULT NULL")
    if conn.execute("SELECT COUNT(*) FROM prompt_layouts").fetchone()[0] == 0:
        row = None
        if "shared_system_prompt" in settings:
            row = conn.execute("SELECT shared_system_prompt FROM settings WHERE id = 1").fetchone()
        main_prompt = (row[0] if row else "") or ""
        conn.execute("INSERT INTO prompt_layouts (id, name) VALUES ('default', 'Default')")
        conn.executemany(
            "INSERT INTO prompt_blocks (id, layout_id, kind, label, content, region, sort_order)"
            " VALUES (?, 'default', ?, ?, ?, ?, ?)",
            [
                (f"default:{key}", kind, label, main_prompt if kind == "main_prompt" else text, region, index)
                for index, (key, kind, label, region, text) in enumerate(_BLOCKS)
            ],
        )
    for table, column in _DROPPED:
        if column in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")  # nosec B608 -- fixed identifiers
    conn.commit()
    print("[migrations] 0097: prompt layouts added; the system prompt is the default layout's Main prompt")
