"""Migration 0066: decision fragments — authoring, snapshots, classifier config.

Three groups of columns, all additive and all nullable or defaulted, so an
upgraded database has no decisions and behaves exactly as before:

* ``interactive_fragments`` gains the eight authoring columns;
* ``messages`` gains the per-reply evaluation record and decision cooldown
  snapshot;
* ``settings`` gains the classifier configuration and this machine's per-card
  approvals.
"""

from __future__ import annotations

import sqlite3

_FRAGMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_type", "TEXT DEFAULT NULL"),
    ("decision_placement", "TEXT DEFAULT NULL"),
    ("decision_state_template", "TEXT DEFAULT NULL"),
    ("decision_instructions", "TEXT DEFAULT NULL"),
    ("decision_criteria", "TEXT DEFAULT NULL"),
    ("decision_outputs", "TEXT DEFAULT NULL"),
    ("decision_resolution", "TEXT DEFAULT NULL"),
    ("decision_threshold", "REAL DEFAULT NULL"),
)

_MESSAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_evaluations", "TEXT NOT NULL DEFAULT '{}'"),
    ("decision_cooldowns", "TEXT NOT NULL DEFAULT '{}'"),
)

_SETTINGS_COLUMNS: tuple[tuple[str, str], ...] = (
    # No FK on the ALTER: SQLite cannot add a REFERENCES clause to an existing
    # table without a full rebuild, and a dangling id resolves to "no classifier
    # configured", which is the same state a fresh install starts in.
    ("decision_endpoint_id", "INTEGER DEFAULT NULL"),
    ("decision_model", "TEXT NOT NULL DEFAULT 'typesafe/jev-1.13'"),
    ("decision_url", "TEXT NOT NULL DEFAULT ''"),
    ("decision_config_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("decision_card_approvals", "TEXT NOT NULL DEFAULT '{}'"),
)


def _add_missing(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> int:
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- module literal
    if not existing:
        return 0
    added = 0
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")  # nosec B608 -- names/DDL are module literals
            added += 1
    return added


def migrate(conn: sqlite3.Connection) -> None:
    added = (
        _add_missing(conn, "interactive_fragments", _FRAGMENT_COLUMNS)
        + _add_missing(conn, "messages", _MESSAGE_COLUMNS)
        + _add_missing(conn, "settings", _SETTINGS_COLUMNS)
    )
    conn.commit()
    print(f"[migrations] 0066: added {added} decision column(s)")
