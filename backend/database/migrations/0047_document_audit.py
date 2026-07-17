"""
0047_document_audit -- add the Document-mode Output Auditor settings columns:
the auto-audit master switch (default on: the scan is local and free), the
auto-patch opt-in (default off: an extra LLM call), and the doc-owned
per-scanner toggle map. The map deliberately duplicates the doc-applicable
subset of editor_audit_toggles rather than sharing the chat column, so a
doc-mode save can never silently re-enable or flip a chat scanner. Mirrors
migration 0022.
"""

from __future__ import annotations

import sqlite3

_TOGGLES_DEFAULT = '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,"contrastive_negation":true}'


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "document_audit_enabled" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN document_audit_enabled INTEGER NOT NULL DEFAULT 1")
        print("[migrations] 0047: added document_audit_enabled column to settings")
    if "document_audit_autopatch" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN document_audit_autopatch INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0047: added document_audit_autopatch column to settings")
    if "document_audit_toggles" not in cols:
        conn.execute(f"ALTER TABLE settings ADD COLUMN document_audit_toggles TEXT NOT NULL DEFAULT '{_TOGGLES_DEFAULT}'")
        print("[migrations] 0047: added document_audit_toggles column to settings")
