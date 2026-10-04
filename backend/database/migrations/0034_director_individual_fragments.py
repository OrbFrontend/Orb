"""Add the ``director_individual_fragments`` settings flag.

Fresh databases get the column from ``schema.py``; this backfills existing
ones. Default 0 keeps the director's single combined tool call.
"""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("settings", "director_individual_fragments INTEGER NOT NULL DEFAULT 0", migration="0034")
