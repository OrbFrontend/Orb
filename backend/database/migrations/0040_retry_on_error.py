"""Add transient retry settings: disabled, 10 retries, 5s delay by default. Retryable status codes remain in inference/retry.py."""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration(
    "settings",
    "retry_enabled INTEGER NOT NULL DEFAULT 0",
    "retry_count INTEGER NOT NULL DEFAULT 10",
    "retry_delay_seconds REAL NOT NULL DEFAULT 5",
    migration="0040",
)
