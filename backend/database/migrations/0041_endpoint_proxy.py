"""Add per-endpoint proxy_url; empty means direct. Validate schemes at the API layer."""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("endpoints", "proxy TEXT NOT NULL DEFAULT ''", migration="0041")
