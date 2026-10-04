"""Add the agentic_lorebook_enabled flag for Director-selected lorebook entries."""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("settings", "agentic_lorebook_enabled INTEGER NOT NULL DEFAULT 0", migration="0030")
