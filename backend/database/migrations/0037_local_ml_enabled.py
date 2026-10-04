"""0037_local_ml_enabled -- add the per-feature local-ML on/off map to settings.

`local_ml_enabled` (default '{}') holds a `{feature: bool}` map where a missing key means enabled, mirroring `workflow_enabled`.
Written only via a per-key json_set (set_local_ml_enabled), never the whole column.
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("settings", "local_ml_enabled TEXT NOT NULL DEFAULT '{}'", migration="0037")
