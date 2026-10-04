"""0011_add_show_editor_diff -- add show_editor_diff column to settings for databases created before this toggle existed.
Default 1 preserves prior behavior (editor diff highlights visible).
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("settings", "show_editor_diff INTEGER NOT NULL DEFAULT 1", migration="0011")
