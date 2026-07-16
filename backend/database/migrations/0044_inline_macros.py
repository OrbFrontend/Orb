"""Migration 0044: inline-macro columns for {{random}} stability.

``director_state.macro_choices`` — per-conversation {{random}} picks for
mood/interactive fragment text: a JSON map of
"surface:fragment_id:macro_text:ordinal" → chosen option (see
``core.macros.resolve_stored_random``), so a fragment's random macros resolve
once per conversation and stay fixed.

``conversations.macro_seed`` — the seed for seeded {{random}} resolution in
per-turn-rebuilt prompt fields (persona, scenario). Empty means "use the
conversation's own id"; checkpoint and compress copies carry the source's
effective seed so seeded picks stay byte-identical to the copied history.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    director_cols = {row[1] for row in conn.execute("PRAGMA table_info(director_state)").fetchall()}
    if "macro_choices" not in director_cols:
        conn.execute("ALTER TABLE director_state ADD COLUMN macro_choices TEXT NOT NULL DEFAULT '{}'")
        print("[migrations] 0044: added macro_choices column to director_state")
    conv_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversations)").fetchall()}
    if "macro_seed" not in conv_cols:
        conn.execute("ALTER TABLE conversations ADD COLUMN macro_seed TEXT NOT NULL DEFAULT ''")
        print("[migrations] 0044: added macro_seed column to conversations")
    conn.commit()
