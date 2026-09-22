"""Migration 0066: the complete decision-fragments schema.

The feature has not shipped, so the authoring, evaluation, classifier, and
enhanced decision columns are kept in one migration. An upgraded database has
no decisions and behaves exactly as before:

* ``interactive_fragments`` gains the authoring and confidence-gating columns;
* ``messages`` gains the per-reply evaluation record and decision cooldown
  snapshot;
* ``settings`` gains the classifier configuration and this machine's per-card
  approvals, including its endpoint foreign key;
* the disabled ``outcome`` seed is added when it is missing.
"""

from __future__ import annotations

import json
import sqlite3

from backend.database import schema

from ..seeds import SEED_INTERACTIVE_FRAGMENTS

_FRAGMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_type", "TEXT DEFAULT NULL"),
    ("decision_placement", "TEXT DEFAULT NULL"),
    ("decision_state_template", "TEXT DEFAULT NULL"),
    ("decision_instructions", "TEXT DEFAULT NULL"),
    ("decision_criteria", "TEXT DEFAULT NULL"),
    ("decision_outputs", "TEXT DEFAULT NULL"),
    ("decision_resolution", "TEXT DEFAULT NULL"),
    ("decision_threshold", "REAL DEFAULT NULL"),
    ("decision_facets", "TEXT DEFAULT NULL"),
    ("decision_confidence_floor", "REAL DEFAULT NULL"),
)

_MESSAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_evaluations", "TEXT NOT NULL DEFAULT '{}'"),
    ("decision_cooldowns", "TEXT NOT NULL DEFAULT '{}'"),
)

_SETTINGS_COLUMNS: tuple[tuple[str, str], ...] = (
    # The FK is restored by _rebuild_settings after the additive upgrade.
    # SQLite cannot add a REFERENCES clause to an existing table with ALTER.
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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def _has_decision_endpoint_fk(conn: sqlite3.Connection) -> bool:
    return any(
        row[2] == "endpoints" and row[3] == "decision_endpoint_id" and row[4] == "id" and row[6].upper() == "SET NULL"
        for row in conn.execute("PRAGMA foreign_key_list(settings)").fetchall()
    )


def _rebuild_settings(conn: sqlite3.Connection) -> None:
    """Rebuild settings from canonical DDL so the decision endpoint is an FK."""
    block = schema.table_create_sql("settings")
    conn.execute(block.replace("CREATE TABLE IF NOT EXISTS settings", "CREATE TABLE settings_0066_new", 1))
    new_columns = {row[1] for row in conn.execute("PRAGMA table_info(settings_0066_new)").fetchall()}
    old_columns = [row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()]
    columns = ", ".join(column for column in old_columns if column in new_columns)
    conn.execute(
        f"INSERT INTO settings_0066_new ({columns}) SELECT {columns} FROM settings"  # nosec B608 -- names from canonical DDL
    )
    conn.execute("DROP TABLE settings")
    conn.execute("ALTER TABLE settings_0066_new RENAME TO settings")


def _seed_outcome(conn: sqlite3.Connection) -> None:
    fragment_columns = _columns(conn, "interactive_fragments")
    seed_columns = {
        "id",
        "label",
        "description",
        "field_type",
        "required",
        "enabled",
        "injection_label",
        "sort_order",
        "decision_type",
        "decision_placement",
        "decision_state_template",
        "decision_instructions",
        "decision_criteria",
        "decision_outputs",
        "decision_resolution",
        "decision_threshold",
        "decision_facets",
        "decision_confidence_floor",
    }
    if (
        not fragment_columns
        or not seed_columns <= fragment_columns
        or conn.execute("SELECT 1 FROM interactive_fragments WHERE id = 'outcome'").fetchone() is not None
    ):
        return

    outcome = next(row for row in SEED_INTERACTIVE_FRAGMENTS if row["id"] == "outcome")
    conn.execute(
        "INSERT INTO interactive_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order, "
        "decision_type, decision_placement, decision_state_template, decision_instructions, decision_criteria, decision_outputs, "
        "decision_resolution, decision_threshold, decision_facets, decision_confidence_floor) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            outcome["id"],
            outcome["label"],
            outcome["description"],
            outcome["field_type"],
            0,
            0,
            outcome["injection_label"],
            outcome["sort_order"],
            outcome["decision_type"],
            outcome["decision_placement"],
            outcome["decision_state_template"],
            outcome["decision_instructions"],
            json.dumps(outcome["decision_criteria"], ensure_ascii=False),
            json.dumps(outcome["decision_outputs"], ensure_ascii=False),
            outcome["decision_resolution"],
            None,
            None,
            None,
        ),
    )


def _cleanup_legacy_shape(conn: sqlite3.Connection) -> tuple[bool, bool]:
    """Remove the pre-squash fallback column and restore the settings FK."""
    needs_column_cleanup = "decision_default" in _columns(conn, "interactive_fragments")
    needs_settings_rebuild = bool(_columns(conn, "settings")) and not _has_decision_endpoint_fk(conn)
    if not (needs_column_cleanup or needs_settings_rebuild):
        return False, False

    # DROP COLUMN and DROP/RENAME both require foreign-key enforcement to be off
    # during the shape change. Commit first because SQLite ignores this pragma
    # while a transaction is active.
    conn.commit()
    had_foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        if needs_column_cleanup:
            conn.execute("ALTER TABLE interactive_fragments DROP COLUMN decision_default")
        if needs_settings_rebuild:
            _rebuild_settings(conn)
        conn.commit()
    finally:
        conn.execute(f"PRAGMA foreign_keys = {had_foreign_keys}")
    return needs_column_cleanup, needs_settings_rebuild


def migrate(conn: sqlite3.Connection) -> None:
    added = (
        _add_missing(conn, "interactive_fragments", _FRAGMENT_COLUMNS)
        + _add_missing(conn, "messages", _MESSAGE_COLUMNS)
        + _add_missing(conn, "settings", _SETTINGS_COLUMNS)
    )
    _seed_outcome(conn)
    conn.commit()
    dropped_legacy_column, rebuilt_settings = _cleanup_legacy_shape(conn)
    print(
        f"[migrations] 0066: added {added} decision column(s), "
        f"dropped legacy fallback={dropped_legacy_column}, rebuilt settings={rebuilt_settings}"
    )
