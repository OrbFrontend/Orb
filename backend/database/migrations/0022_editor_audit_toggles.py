"""
0022_editor_audit_toggles -- add editor_audit_toggles column to settings so the
Output Auditor can enable/disable individual scanners. Default has every
scanner on, preserving the prior behavior where all audits ran unconditionally.
"""

from __future__ import annotations

import sqlite3

_DEFAULT = (
    '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,'
    '"contrastive_negation":true,"phrase_repetition":true,"structural_repetition":true}'
)


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "editor_audit_toggles" not in cols:
        conn.execute(f"ALTER TABLE settings ADD COLUMN editor_audit_toggles TEXT NOT NULL DEFAULT '{_DEFAULT}'")
        print("[migrations] 0022: added editor_audit_toggles column to settings")
