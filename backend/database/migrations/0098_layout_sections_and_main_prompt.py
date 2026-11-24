"""Move the card sections into ordinary layout blocks and the system prompt into each layout's Main prompt.

The Character, Scenario, Example dialogue, Chat instructions and User persona markers become custom blocks whose text names
their section placeholder; engine markers drop their stored templates; every Main prompt still holding its template takes the
global system prompt; the system prompt settings and the model configs' system prompts are dropped. A rerun changes nothing.
"""

from __future__ import annotations

import sqlite3

# A snapshot of core.prompt_layout's section blocks; later changes do not rewrite existing rows.
_SECTIONS = {
    "character": "## Character: {{char}}\n{{description}}\n\n{{personality}}",
    "scenario": "## Scenario\n{{scenario}}",
    "example_dialogue": "## Example Dialogue\n{{examples}}",
    "card_instructions": "## Additional Instructions\n{{instructions}}",
    "user_persona": "## User: {{user}}\n{{persona}}",
}
_DROPPED = (
    ("settings", "shared_system_prompt"),
    ("settings", "system_prompt"),
    ("settings", "agent_shared_system_prompt"),
    ("model_configs", "system_prompt"),
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- fixed table names


def migrate(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "UPDATE prompt_blocks SET kind = 'custom', content = ?, role = 'system' WHERE kind = ?",
        [(text, kind) for kind, text in _SECTIONS.items()],
    )
    if "shared_system_prompt" in _columns(conn, "settings"):
        row = conn.execute("SELECT shared_system_prompt FROM settings WHERE id = 1").fetchone()
        conn.execute(
            "UPDATE prompt_blocks SET content = ? WHERE kind = 'main_prompt' AND content = '{{content}}'",
            (row[0] if row else "",),
        )
    conn.execute("UPDATE prompt_blocks SET content = '' WHERE kind NOT IN ('custom', 'main_prompt')")
    for table, column in _DROPPED:
        if column in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")  # nosec B608 -- fixed identifiers
    conn.commit()
    print("[migrations] 0098: card sections are layout blocks; the system prompt is each layout's Main prompt")
