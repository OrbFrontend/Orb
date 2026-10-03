"""Seed the disabled suggestions feedback fragment if absent."""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    existing = conn.execute("SELECT 1 FROM interactive_fragments WHERE id = 'suggested_actions'").fetchone()
    if existing:
        return

    conn.execute(
        """
        INSERT INTO interactive_fragments
            (id, label, description, field_type, required, enabled, injection_label, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "suggested_actions",
            "Suggestions",
            "Suggest 2 fresh, distinct actions the user could do next. Be concise, 2 sentences max.",
            "feedback",
            0,  # not required
            0,  # seeded disabled
            "Suggestions",
            7,
        ),
    )
    conn.commit()
    print("[migrations] 0025: inserted 'suggested_actions' feedback fragment")
