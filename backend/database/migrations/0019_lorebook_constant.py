"""0019_lorebook_constant -- add `constant` column to lorebook_entries.

When set, the entry is always injected regardless of keyword matches.
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("lorebook_entries", "constant BOOLEAN NOT NULL DEFAULT 0", migration="0019")
