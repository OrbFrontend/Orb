"""Add dependent decision facets and confidence gating."""

from __future__ import annotations

import sqlite3

from ..seeds import SEED_INTERACTIVE_FRAGMENTS


def migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(interactive_fragments)").fetchall()}
    added = 0
    if columns and "decision_facets" not in columns:
        conn.execute("ALTER TABLE interactive_fragments ADD COLUMN decision_facets TEXT DEFAULT NULL")
        added += 1
    if columns and "decision_confidence_floor" not in columns:
        conn.execute("ALTER TABLE interactive_fragments ADD COLUMN decision_confidence_floor REAL DEFAULT NULL")
        added += 1
    outcome = next(row for row in SEED_INTERACTIVE_FRAGMENTS if row["id"] == "outcome")
    if columns and conn.execute("SELECT 1 FROM interactive_fragments WHERE id = 'outcome'").fetchone() is None:
        import json

        conn.execute(
            "INSERT INTO interactive_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order, "
            "decision_type, decision_placement, decision_state_template, decision_instructions, decision_criteria, decision_outputs, "
            "decision_default, decision_resolution, decision_threshold, decision_facets, decision_confidence_floor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                outcome["id"],
                outcome["label"],
                outcome["description"],
                outcome["field_type"],
                0,
                0,
                outcome["injection_label"],
                outcome["sort_order"],
                outcome["decision_type"],
                outcome["decision_placement"],
                outcome["decision_state_template"],
                outcome["decision_instructions"],
                json.dumps(outcome["decision_criteria"], ensure_ascii=False),
                json.dumps(outcome["decision_outputs"], ensure_ascii=False),
                outcome["decision_default"],
                outcome["decision_resolution"],
                None,
                None,
                None,
            ),
        )
    conn.commit()
    print(f"[migrations] 0067: added {added} enhanced decision column(s)")
