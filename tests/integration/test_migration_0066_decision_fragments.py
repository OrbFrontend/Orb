"""Cover decision-fragment schema upgrades and legacy endpoint migration."""

from __future__ import annotations

import importlib
import sqlite3

from backend.database import schema

_MIGRATION = importlib.import_module("backend.database.migrations.0066_decision_fragments")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]  # nosec B608 -- literal table names


def _pre_0066(*, legacy: tuple[str, ...] = ()) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE interactive_fragments (
            id TEXT PRIMARY KEY,
            label TEXT,
            description TEXT,
            field_type TEXT,
            required INTEGER DEFAULT 0,
            enabled INTEGER DEFAULT 1,
            injection_label TEXT,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE messages (id INTEGER PRIMARY KEY);
        INSERT INTO messages VALUES (1);
        CREATE TABLE endpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT '',
            active_model_config_id INTEGER,
            agent_active_model_config_id INTEGER,
            completion_mode TEXT NOT NULL DEFAULT 'chat',
            proxy TEXT NOT NULL DEFAULT ''
        );
        """
    )
    conn.executescript(schema.table_create_sql("settings"))
    for column in legacy:
        conn.execute(f"ALTER TABLE interactive_fragments ADD COLUMN {column} TEXT DEFAULT NULL")  # nosec B608 -- test literals
    conn.commit()
    return conn


def _borrowed_judge(conn: sqlite3.Connection, *, override: str = "") -> int:
    """Build a legacy schema where the classifier borrowed a chat endpoint and route."""
    conn.execute("ALTER TABLE settings ADD COLUMN decision_url TEXT NOT NULL DEFAULT ''")
    cursor = conn.execute(
        "INSERT INTO endpoints (url, api_key, proxy) VALUES ('https://openrouter.ai/api/v1', 'writer-key', 'socks5://127.0.0.1:1080')"
    )
    endpoint_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO settings (id, endpoint_url, model_name, decision_endpoint_id, decision_url, decision_model) "
        "VALUES (1, 'http://localhost:5000/v1', 'default', ?, ?, 'typesafe/jev-1.13')",
        (endpoint_id, override),
    )
    conn.commit()
    assert endpoint_id is not None
    return endpoint_id


def test_adds_the_decision_columns_and_seeds_the_outcome_fragment():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)

    columns = _columns(conn, "interactive_fragments")
    assert "decision_type" in columns
    assert "decision_confidence_floor" in columns
    assert conn.execute("SELECT enabled FROM interactive_fragments WHERE id = 'outcome'").fetchone() == (0,)
    assert conn.execute("SELECT decision_evaluations FROM messages").fetchone() == ("{}",)


def test_the_facet_column_is_never_created():
    conn = _pre_0066()
    _MIGRATION.migrate(conn)

    assert "decision_facets" not in _columns(conn, "interactive_fragments")


def test_columns_cut_before_release_are_dropped_from_a_development_database():
    conn = _pre_0066(legacy=("decision_default", "decision_facets"))
    _MIGRATION.migrate(conn)

    columns = _columns(conn, "interactive_fragments")
    assert "decision_default" not in columns
    assert "decision_facets" not in columns


def test_migration_is_idempotent():
    conn = _pre_0066(legacy=("decision_facets",))
    _MIGRATION.migrate(conn)
    _MIGRATION.migrate(conn)

    assert _columns(conn, "interactive_fragments").count("decision_type") == 1
    assert conn.execute("SELECT count(*) FROM interactive_fragments WHERE id = 'outcome'").fetchone() == (1,)


# ── judge endpoints ──────────────────────────────────────────────────────────


def test_endpoints_gain_a_lane_and_existing_rows_are_chat():
    conn = _pre_0066()
    conn.execute("INSERT INTO endpoints (url) VALUES ('https://openrouter.ai/api/v1')")
    _MIGRATION.migrate(conn)

    assert "kind" in _columns(conn, "endpoints")
    assert conn.execute("SELECT kind FROM endpoints").fetchall() == [("chat",)]


def test_a_borrowed_chat_endpoint_becomes_the_classifiers_own_judge_row():
    conn = _pre_0066()
    writer_id = _borrowed_judge(conn)
    _MIGRATION.migrate(conn)

    judge_id = conn.execute("SELECT decision_endpoint_id FROM settings WHERE id = 1").fetchone()[0]
    assert judge_id != writer_id
    # The credentials and proxy come along: the classifier was reaching the
    # gateway with them before the upgrade and must still reach it after.
    assert conn.execute("SELECT url, api_key, proxy, kind FROM endpoints WHERE id = ?", (judge_id,)).fetchone() == (
        "https://openrouter.ai/api/v1",
        "writer-key",
        "socks5://127.0.0.1:1080",
        "judge",
    )
    assert conn.execute("SELECT kind FROM endpoints WHERE id = ?", (writer_id,)).fetchone() == ("chat",)


def test_a_route_override_becomes_the_judge_endpoints_url():
    conn = _pre_0066()
    _borrowed_judge(conn, override="https://gw.test/v2/judge/decisions")
    _MIGRATION.migrate(conn)

    judge_id = conn.execute("SELECT decision_endpoint_id FROM settings WHERE id = 1").fetchone()[0]
    assert conn.execute("SELECT url FROM endpoints WHERE id = ?", (judge_id,)).fetchone() == (
        "https://gw.test/v2/judge/decisions",
    )
    assert "decision_url" not in _columns(conn, "settings")


def test_adopting_the_judge_endpoint_is_idempotent():
    conn = _pre_0066()
    _borrowed_judge(conn)
    _MIGRATION.migrate(conn)
    first = conn.execute("SELECT decision_endpoint_id FROM settings WHERE id = 1").fetchone()[0]
    _MIGRATION.migrate(conn)

    assert conn.execute("SELECT decision_endpoint_id FROM settings WHERE id = 1").fetchone()[0] == first
    assert conn.execute("SELECT count(*) FROM endpoints WHERE kind = 'judge'").fetchone() == (1,)
