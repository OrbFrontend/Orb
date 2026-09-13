"""Seed the disabled Humanize Dialogue post-processing fragment."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'interactive_fragments'").fetchone():
        return
    if conn.execute("SELECT 1 FROM interactive_fragments WHERE id = 'humanize_dialogue'").fetchone():
        return

    conn.execute(
        "INSERT INTO interactive_fragments "
        "(id, label, description, field_type, required, enabled, injection_label, sort_order, direction_note_timing) "
        "VALUES ('humanize_dialogue', 'Humanize Dialogue', ?, 'post_processing', 0, 0, "
        "'Humanize Dialogue', 7, 'post_turn')",
        (
            "Rewrite spoken dialogue to sound human: more casual, and natural. Remove slop and fluff. Preserve meaning, "
            "characterization, subtext, tone, plot facts, and dialogue markup. Change dialogue only; do not "
            "rewrite narration or add new events.",
        ),
    )
    print("[migrations] 0061: seeded the disabled 'humanize_dialogue' post-processing fragment")
