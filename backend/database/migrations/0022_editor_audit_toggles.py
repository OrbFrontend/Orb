"""0022_editor_audit_toggles -- add editor_audit_toggles column to settings so the Output Auditor can enable/disable individual
scanners. Default has every scanner on, preserving the prior behavior where all audits ran unconditionally.
"""

from __future__ import annotations

from .helpers import column_migration

_DEFAULT = (
    '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,'
    '"contrastive_negation":true,"phrase_repetition":true,"structural_repetition":true}'
)


migrate = column_migration("settings", f"editor_audit_toggles TEXT NOT NULL DEFAULT '{_DEFAULT}'", migration="0022")
