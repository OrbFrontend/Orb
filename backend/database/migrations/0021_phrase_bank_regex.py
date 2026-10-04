"""Add phrase-bank kind and pattern columns; existing groups default to literal."""

from __future__ import annotations

from .helpers import column_migration

migrate = column_migration("phrase_bank", "kind TEXT NOT NULL DEFAULT 'literal'", "pattern TEXT", migration="0021")
