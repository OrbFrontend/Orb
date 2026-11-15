"""Add the assistant panel's options as settings: its system prompt opening, Think, and the tool groups switched off."""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration(
    "settings",
    "assistant_instructions TEXT NOT NULL DEFAULT ''",
    "assistant_reasoning INTEGER NOT NULL DEFAULT 0",
    "assistant_tool_groups TEXT NOT NULL DEFAULT '{}'",
    migration="0092",
)
