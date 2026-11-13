"""Add the Document-mode Prose Rewriter switch, off by default and separate from the chat workflow toggle."""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("settings", "document_prose_rewriter_enabled INTEGER NOT NULL DEFAULT 0", migration="0089")
