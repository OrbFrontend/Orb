"""0051_lorebook_at_depth -- `@ Depth` placement for constant entries.

A constant entry with `at_depth` rides the per-turn tail (after the user message, macros re-resolved every turn) instead of the
cached system prefix. Defaults to 0, so existing entries keep their prefix placement.
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("lorebook_entries", "at_depth INTEGER NOT NULL DEFAULT 0", migration="0051")
