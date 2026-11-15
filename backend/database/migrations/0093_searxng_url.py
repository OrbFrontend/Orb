"""Add the SearXNG server the assistant's web search asks."""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("settings", "searxng_url TEXT NOT NULL DEFAULT ''", migration="0093")
