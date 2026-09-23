"""Migration 0066: the complete decision-fragments schema.

The feature has not shipped, so the authoring, evaluation, classifier, endpoint
and enhanced decision columns are kept in one migration. Every step here is
guarded and re-runnable, because that is what amending a single unshipped
migration costs: the runner records migrations by id, so a development database
that already applied this file has to be unstamped by hand
(``DELETE FROM schema_migrations WHERE id = '0066_decision_fragments'``) to pick
up an amendment, and it then replays the whole file. An upgraded database has
no decisions and behaves exactly as before:

* ``interactive_fragments`` gains the authoring and confidence-gating columns;
* ``messages`` gains the per-reply evaluation record and decision cooldown
  snapshot;
* ``settings`` gains the classifier configuration, including its endpoint
  foreign key;
* ``endpoints`` gains ``kind``, which separates the Writer/Agent pool from the
  classifier's own judge rows;
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
    ("decision_confidence_floor", "REAL DEFAULT NULL"),
)

_DECISION_COLUMNS = tuple(name for name, _ in _FRAGMENT_COLUMNS)

# Built on this branch, then cut before release. Dropped if a database has them.
_LEGACY_FRAGMENT_COLUMNS: tuple[str, ...] = ("decision_default", "decision_facets")
# Same story on settings: the classifier's route was a second field that could
# disagree with the endpoint it was derived from. It is now derived from the
# judge endpoint's URL alone, and a database carrying an override folds it into
# that endpoint (see _adopt_judge_endpoint) before the column goes.
_LEGACY_SETTINGS_COLUMNS: tuple[str, ...] = ("decision_url", "decision_config_revision")

_ENDPOINT_COLUMNS: tuple[tuple[str, str], ...] = (("kind", "TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'judge'))"),)

_MESSAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("decision_evaluations", "TEXT NOT NULL DEFAULT '{}'"),
    ("decision_cooldowns", "TEXT NOT NULL DEFAULT '{}'"),
)

_SETTINGS_COLUMNS: tuple[tuple[str, str], ...] = (
    # The FK is restored by _rebuild_settings after the additive upgrade.
    # SQLite cannot add a REFERENCES clause to an existing table with ALTER.
    ("decision_endpoint_id", "INTEGER DEFAULT NULL"),
    ("decision_model", "TEXT NOT NULL DEFAULT 'typesafe/jev-1.13'"),
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
    if (
        not _columns(conn, "interactive_fragments")
        or conn.execute("SELECT 1 FROM interactive_fragments WHERE id = 'outcome'").fetchone()
    ):
        return
    outcome = next(row for row in SEED_INTERACTIVE_FRAGMENTS if row["id"] == "outcome")
    columns = ("id", "label", "description", "field_type", "required", "enabled", "injection_label", "sort_order")
    columns += _DECISION_COLUMNS
    values = [outcome.get(column, "") for column in columns]
    conn.execute(
        f"INSERT INTO interactive_fragments ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",  # nosec B608 -- literals
        [json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for value in values],
    )


def _adopt_judge_endpoint(conn: sqlite3.Connection) -> int | None:
    """Give a configured classifier a judge endpoint of its own.

    The pre-squash shape borrowed a Writer/Agent endpoint for credentials and
    kept the route in ``settings.decision_url``. Both fold into one judge row
    here -- the URL the classifier should have carried all along (the override
    when one was set, the borrowed base otherwise) with that endpoint's key and
    proxy -- so the chat lanes stop listing a row the Judge configured and the
    Judge stops depending on a row the Writer can delete or repoint.

    Returns the judge endpoint's id, or None when there was nothing to adopt.
    """
    settings_columns = _columns(conn, "settings")
    if "decision_endpoint_id" not in settings_columns or not _columns(conn, "endpoints"):
        return None
    has_override = "decision_url" in settings_columns
    projection = "decision_endpoint_id" + (", decision_url" if has_override else "")
    row = conn.execute(f"SELECT {projection} FROM settings WHERE id = 1").fetchone()  # nosec B608 -- literal columns
    if row is None:
        return None
    endpoint_id, override = row[0], (row[1] if has_override else "") or ""
    source = (
        conn.execute("SELECT url, api_key, proxy, kind FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()
        if endpoint_id is not None
        else None
    )
    if source is not None and source[3] == "judge":
        return None  # Already its own row: nothing borrowed, nothing to move.
    url = override or (source[0] if source is not None else "")
    if not url:
        return None
    api_key = source[1] if source is not None else ""
    proxy = source[2] if source is not None else ""
    existing = conn.execute("SELECT id FROM endpoints WHERE kind = 'judge' AND url = ?", (url,)).fetchone()
    if existing is not None:
        judge_id = existing[0]
    else:
        cursor = conn.execute(
            "INSERT INTO endpoints (url, api_key, proxy, kind) VALUES (?, ?, ?, 'judge')", (url, api_key, proxy)
        )
        judge_id = cursor.lastrowid
    conn.execute("UPDATE settings SET decision_endpoint_id = ? WHERE id = 1", (judge_id,))
    return judge_id


def _cleanup_legacy_shape(conn: sqlite3.Connection) -> tuple[list[str], bool]:
    """Remove pre-squash columns and restore the settings FK.

    ``decision_default``, ``decision_facets`` and ``decision_url`` were all built
    and then cut before release, so a development database can carry any of them.
    A settings rebuild drops ``decision_url`` on its own -- it copies only the
    columns the canonical DDL still has -- so the explicit drop is for the case
    where the FK is already in place and no rebuild is due.
    """
    fragment_columns = _columns(conn, "interactive_fragments")
    settings_columns = _columns(conn, "settings")
    stale = [column for column in _LEGACY_FRAGMENT_COLUMNS if column in fragment_columns]
    needs_settings_rebuild = bool(settings_columns) and not _has_decision_endpoint_fk(conn)
    stale_settings = (
        [] if needs_settings_rebuild else [column for column in _LEGACY_SETTINGS_COLUMNS if column in settings_columns]
    )
    if not (stale or stale_settings or needs_settings_rebuild):
        return [], False

    # DROP COLUMN and DROP/RENAME both require foreign-key enforcement to be off
    # during the shape change. Commit first because SQLite ignores this pragma
    # while a transaction is active.
    conn.commit()
    had_foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        for column in stale:
            conn.execute(f"ALTER TABLE interactive_fragments DROP COLUMN {column}")  # nosec B608 -- module literals
        for column in stale_settings:
            conn.execute(f"ALTER TABLE settings DROP COLUMN {column}")  # nosec B608 -- module literals
        if needs_settings_rebuild:
            _rebuild_settings(conn)
        conn.commit()
    finally:
        conn.execute(f"PRAGMA foreign_keys = {had_foreign_keys}")
    return stale + stale_settings, needs_settings_rebuild


def migrate(conn: sqlite3.Connection) -> None:
    added = (
        _add_missing(conn, "interactive_fragments", _FRAGMENT_COLUMNS)
        + _add_missing(conn, "messages", _MESSAGE_COLUMNS)
        + _add_missing(conn, "settings", _SETTINGS_COLUMNS)
        + _add_missing(conn, "endpoints", _ENDPOINT_COLUMNS)
    )
    _seed_outcome(conn)
    # Before the cleanup below drops decision_url: the override is what the judge
    # endpoint's URL should be.
    judge_id = _adopt_judge_endpoint(conn)
    conn.commit()
    dropped, rebuilt_settings = _cleanup_legacy_shape(conn)
    print(
        f"[migrations] 0066: added {added} decision column(s), judge endpoint={judge_id or 'none'}, "
        f"dropped legacy column(s)={dropped or 'none'}, rebuilt settings={rebuilt_settings}"
    )
