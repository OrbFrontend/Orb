"""Add Document audit settings: auto-audit on, auto-patch off, and an independent
scanner-toggle map so document saves cannot alter chat settings.
"""

from __future__ import annotations

from .helpers import column_migration

_TOGGLES_DEFAULT = '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,"contrastive_negation":true}'


migrate = column_migration(
    "settings",
    "document_audit_enabled INTEGER NOT NULL DEFAULT 1",
    "document_audit_autopatch INTEGER NOT NULL DEFAULT 0",
    f"document_audit_toggles TEXT NOT NULL DEFAULT '{_TOGGLES_DEFAULT}'",
    migration="0047",
)
