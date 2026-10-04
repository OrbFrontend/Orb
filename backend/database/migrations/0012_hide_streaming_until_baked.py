"""0012_hide_streaming_until_baked -- add hide_streaming_until_baked column to settings for databases created before this toggle
existed. Default 0 preserves prior behavior (streaming message visible live).
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("settings", "hide_streaming_until_baked INTEGER NOT NULL DEFAULT 0", migration="0012")
