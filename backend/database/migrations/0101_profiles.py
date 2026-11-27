"""Turn prompt layouts into profiles: each profile owns its layout's blocks, a generation config and its own library.

Renames prompt_layouts to profiles and settings.active_prompt_layout_id to active_profile_id, and adds the config JSON and the
lane foreign keys (writer, agent and Judge endpoints; writer and agent model configs). Every profile is filled from the live
state, so switching between former layouts changes only the blocks. 0097 added the active layout setting as NULL and only a
layout switch filled it, so a NULL or stale active id is stored as the profile it read as; the frontend reads the column itself.

Then rebuilds the library tables with a profile_id column and copies every existing row into every profile; a fragment copy keeps
its id. The slop suggestions and the miner's state are derived, so they are rebuilt empty. ``worlds.is_global`` becomes a
``profile_worlds`` row for every profile. A database with library rows but no profile, an old partial backup file made without
its configs, first gets the placeholder profile 'default'.

A database from before this fold may hold a presets table, settings.active_preset_id and ``config.fragments`` enabled maps: a
fragment copy takes ``enabled`` from its profile's map (the active profile's copies, and a fragment the map does not name, keep
the live value), and the maps leave every config. Every step checks the current shape first, so a rerun changes nothing.
"""

from __future__ import annotations

import json
import sqlite3

# A snapshot of the profile config shape at this migration; later changes do not rewrite existing rows.
_SETTINGS = (
    "enable_agent",
    "enabled_tools",
    "length_guard_enabled",
    "length_guard_enforce",
    "length_guard_max_words",
    "length_guard_max_paragraphs",
    "agentic_lorebook_enabled",
    "director_individual_fragments",
    "prevent_prompt_overrides",
    "reasoning_enabled_passes",
    "reasoning_prefill_passes",
    "editor_audit_toggles",
)
_JSON_SETTINGS = frozenset({"enabled_tools", "reasoning_enabled_passes", "reasoning_prefill_passes", "editor_audit_toggles"})
_LIVE_LANES = ("active_endpoint_id", "agent_same_as_writer", "agent_endpoint_id", "decision_endpoint_id", "decision_model")
_LANE_COLUMNS = (
    ("writer_endpoint_id", "endpoints"),
    ("writer_model_config_id", "model_configs"),
    ("agent_endpoint_id", "endpoints"),
    ("agent_model_config_id", "model_configs"),
    ("judge_endpoint_id", "endpoints"),
)
# The active id's earlier names, oldest first.
_OLD_ACTIVE = ("active_prompt_layout_id", "active_preset_id")

# The library tables as this migration builds them; later changes do not rewrite existing rows.
_TABLES: dict[str, str] = {
    "mood_fragments": """CREATE TABLE mood_fragments_0101 (
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        id TEXT NOT NULL,
        label TEXT NOT NULL,
        description TEXT NOT NULL,
        prompt_text TEXT NOT NULL,
        negative_prompt TEXT NOT NULL DEFAULT '',
        cooldown_turns INTEGER NOT NULL DEFAULT 0,
        enabled BOOLEAN NOT NULL DEFAULT 1,
        PRIMARY KEY (profile_id, id)
    )""",
    "interactive_fragments": """CREATE TABLE interactive_fragments_0101 (
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        id TEXT NOT NULL,
        label TEXT NOT NULL,
        description TEXT NOT NULL,
        field_type TEXT NOT NULL DEFAULT 'string',
        required BOOLEAN NOT NULL DEFAULT 0,
        enabled BOOLEAN NOT NULL DEFAULT 1,
        injection_label TEXT NOT NULL,
        sort_order INTEGER NOT NULL DEFAULT 0,
        cooldown_turns INTEGER NOT NULL DEFAULT 0,
        state_mode TEXT DEFAULT NULL CHECK (state_mode IS NULL OR state_mode IN ('value', 'entries')),
        state_update TEXT DEFAULT NULL CHECK (state_update IS NULL OR state_update IN ('after_reply', 'before_writer', 'manual')),
        state_inject TEXT DEFAULT NULL CHECK (state_inject IS NULL OR state_inject IN ('off', 'director', 'writer', 'both')),
        state_max_entries INTEGER DEFAULT NULL CHECK (state_max_entries IS NULL OR state_max_entries >= 0),
        decision_type TEXT DEFAULT NULL,
        decision_inject TEXT DEFAULT NULL CHECK (decision_inject IS NULL OR decision_inject IN ('director', 'writer', 'both')),
        decision_state_template TEXT DEFAULT NULL,
        decision_instructions TEXT DEFAULT NULL,
        decision_criteria TEXT DEFAULT NULL,
        decision_outputs TEXT DEFAULT NULL,
        decision_resolution TEXT DEFAULT NULL,
        decision_threshold REAL DEFAULT NULL,
        decision_confidence_floor REAL DEFAULT NULL,
        gate_question TEXT NOT NULL DEFAULT '',
        gate_replies INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (profile_id, id)
    )""",
    "phrase_bank": """CREATE TABLE phrase_bank_0101 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        variants TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'literal',
        pattern TEXT
    )""",
    "slop_dismissals": """CREATE TABLE slop_dismissals_0101 (
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        pattern TEXT NOT NULL,
        dismissed_at TEXT NOT NULL,
        PRIMARY KEY (profile_id, key)
    )""",
    "slop_suggestions": """CREATE TABLE slop_suggestions_0101 (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
        key TEXT NOT NULL,
        lane TEXT NOT NULL CHECK (lane IN ('new', 'longstanding')),
        label TEXT NOT NULL,
        pattern TEXT NOT NULL,
        stats TEXT NOT NULL DEFAULT '{}',
        fillers TEXT NOT NULL DEFAULT '[]',
        examples TEXT NOT NULL DEFAULT '[]',
        mined_at TEXT NOT NULL,
        UNIQUE (profile_id, key)
    )""",
    "slop_mining_state": """CREATE TABLE slop_mining_state_0101 (
        profile_id TEXT PRIMARY KEY REFERENCES profiles(id) ON DELETE CASCADE,
        last_run_at TEXT,
        last_status TEXT NOT NULL DEFAULT ''
    )""",
}
# Derived tables: rebuilt empty, since what they held was mined against the one shared bank.
_DERIVED = frozenset({"slop_suggestions", "slop_mining_state"})
# The tables whose rows the placeholder profile is created to hold.
_LIBRARY = ("mood_fragments", "interactive_fragments", "phrase_bank", "slop_dismissals")
_FRAGMENT_MAPS = (("mood_fragments", "mood"), ("interactive_fragments", "interactive"))
_PROFILE_WORLDS = """CREATE TABLE IF NOT EXISTS profile_worlds (
    profile_id TEXT NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    PRIMARY KEY (profile_id, world_id)
)"""


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]  # nosec B608 -- fixed table names


def _existing(conn: sqlite3.Connection, table: str, value: object) -> int | None:
    """*value* when *table* has that id, else None: a lane column never names a missing row."""
    if not isinstance(value, int):
        return None
    found = conn.execute(f"SELECT 1 FROM {table} WHERE id = ?", (value,)).fetchone()  # nosec B608 -- fixed table names
    return value if found else None


def _endpoint_config(conn: sqlite3.Connection, endpoint_id: int | None, column: str) -> int | None:
    if endpoint_id is None:
        return None
    row = conn.execute(f"SELECT {column} FROM endpoints WHERE id = ?", (endpoint_id,)).fetchone()  # nosec B608 -- fixed
    return row[0] if row else None


def _live_state(conn: sqlite3.Connection, tables: set[str]) -> tuple[dict, list[int | None]]:
    """The live config and lane column values; an empty config and no lanes without a settings row."""
    row = None
    if "settings" in tables:
        row = conn.execute(f"SELECT {', '.join(_SETTINGS + _LIVE_LANES)} FROM settings WHERE id = 1").fetchone()  # nosec B608
    if row is None:
        return {}, [None] * len(_LANE_COLUMNS)
    values = dict(zip(_SETTINGS + _LIVE_LANES, row, strict=True))
    writer_id, agent_id = values["active_endpoint_id"], values["agent_endpoint_id"]
    lanes = (
        writer_id,
        _endpoint_config(conn, writer_id, "active_model_config_id"),
        agent_id,
        _endpoint_config(conn, agent_id, "agent_active_model_config_id"),
        values["decision_endpoint_id"],
    )
    config = {
        "settings": {key: json.loads(values[key] or "{}") if key in _JSON_SETTINGS else values[key] for key in _SETTINGS},
        "models": {"agent_same_as_writer": values["agent_same_as_writer"], "judge_model": values["decision_model"]},
    }
    return config, [_existing(conn, parent, value) for (_, parent), value in zip(_LANE_COLUMNS, lanes, strict=True)]


def _rename_active(conn: sqlite3.Connection) -> None:
    """Leave settings with active_profile_id alone, holding the first id any of the earlier names held."""
    columns = _columns(conn, "settings")
    old = [column for column in _OLD_ACTIVE if column in columns]
    if not old:
        return
    if "active_profile_id" not in columns:
        conn.execute(f"ALTER TABLE settings RENAME COLUMN {old.pop(0)} TO active_profile_id")  # nosec B608 -- fixed names
    for column in old:
        # An earlier settings rebuild used a later shape, so more than one name is here.
        conn.execute(f"UPDATE settings SET active_profile_id = COALESCE(active_profile_id, {column})")  # nosec B608
        conn.execute(f"ALTER TABLE settings DROP COLUMN {column}")  # nosec B608 -- fixed names


def _make_profiles(conn: sqlite3.Connection) -> None:
    """Rename the layouts table to profiles, add the config and lane columns, and fill them from the live state."""
    tables = _tables(conn)
    renamed = None
    for old in ("prompt_layouts", "presets"):
        if old in tables and "profiles" not in tables:
            # Off, the rename rewrites the prompt_blocks foreign key to name the new table; some Python builds default it on.
            conn.execute("PRAGMA legacy_alter_table = OFF")
            conn.execute(f"ALTER TABLE {old} RENAME TO profiles")  # nosec B608 -- fixed names
            (blocks_sql,) = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'prompt_blocks'"
            ).fetchone()
            assert old not in blocks_sql, f"prompt_blocks still references {old} after the rename"
            tables = (tables - {old}) | {"profiles"}
            renamed = old
    if "profiles" not in tables:
        return
    have = _columns(conn, "profiles")
    if "config" not in have:
        conn.execute("ALTER TABLE profiles ADD COLUMN config TEXT NOT NULL DEFAULT '{}'")
    for column, parent in _LANE_COLUMNS:
        if column not in have:
            conn.execute(
                f"ALTER TABLE profiles ADD COLUMN {column} INTEGER REFERENCES {parent}(id) ON DELETE SET NULL"  # nosec B608
            )
    if "settings" in tables:
        _rename_active(conn)
        # The order get_active_profile_id falls back on: 'default' first, then by name.
        conn.execute(
            """UPDATE settings SET active_profile_id = (
                   SELECT id FROM profiles ORDER BY id != 'default', name COLLATE NOCASE LIMIT 1
               )
               WHERE id = 1 AND EXISTS (SELECT 1 FROM profiles)
                 AND (active_profile_id IS NULL OR active_profile_id NOT IN (SELECT id FROM profiles))"""
        )
    if renamed == "prompt_layouts":
        config, lanes = _live_state(conn, tables)
        conn.execute(
            f"UPDATE profiles SET config = ?, {', '.join(f'{c} = ?' for c, _ in _LANE_COLUMNS)}",  # nosec B608 -- fixed names
            (json.dumps(config, ensure_ascii=False), *lanes),
        )
    conn.commit()


def _decoded(raw: str | None) -> dict:
    try:
        config = json.loads(raw or "{}")
    except ValueError:
        return {}
    return config if isinstance(config, dict) else {}


def _has_library_rows(conn: sqlite3.Connection, tables: set[str]) -> bool:
    for table in _LIBRARY:
        if table in tables and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():  # nosec B608 -- fixed names
            return True
    return (
        "worlds" in tables
        and "is_global" in _columns(conn, "worlds")
        and bool(conn.execute("SELECT 1 FROM worlds WHERE is_global = 1 LIMIT 1").fetchone())
    )


def _active_profile(conn: sqlite3.Connection, tables: set[str]) -> str | None:
    if "settings" in tables and "active_profile_id" in _columns(conn, "settings"):
        row = conn.execute(
            "SELECT p.id FROM settings s JOIN profiles p ON p.id = s.active_profile_id WHERE s.id = 1"
        ).fetchone()
        if row:
            return str(row[0])
    row = conn.execute("SELECT id FROM profiles ORDER BY id != 'default', name COLLATE NOCASE LIMIT 1").fetchone()
    return str(row[0]) if row else None


def _rebuild(conn: sqlite3.Connection, table: str) -> None:
    """Rebuild *table* with profile_id, copying each row into every profile (none for a derived table)."""
    old = _columns(conn, table)
    conn.execute(_TABLES[table])
    new = _columns(conn, f"{table}_0101")
    if table not in _DERIVED:
        # phrase_bank's surrogate id is renumbered: each profile's copy is a row of its own, in the bank's order.
        shared = [c for c in new if c in old and c != "profile_id" and not (table == "phrase_bank" and c == "id")]
        names = ", ".join(shared)
        selected = ", ".join(f"t.{c}" for c in shared)
        conn.execute(
            f"INSERT INTO {table}_0101 (profile_id, {names}) "  # nosec B608 -- names from the fixed DDL above
            f"SELECT p.id, {selected} FROM {table} t CROSS JOIN profiles p ORDER BY p.rowid, t.rowid"
        )
    conn.execute(f"DROP TABLE {table}")  # nosec B608 -- fixed names
    conn.execute(f"ALTER TABLE {table}_0101 RENAME TO {table}")  # nosec B608 -- fixed names


def _split_libraries(conn: sqlite3.Connection) -> str:
    """Give every profile its own library rows and global Worlds; the rebuilt tables, for the log."""
    tables = _tables(conn)
    had_fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    try:
        conn.execute("BEGIN")
        if not conn.execute("SELECT 1 FROM profiles LIMIT 1").fetchone() and _has_library_rows(conn, tables):
            conn.execute("INSERT INTO profiles (id, name, config) VALUES ('default', 'Default', '{}')")
        active = _active_profile(conn, tables)
        configs = {str(pid): _decoded(raw) for pid, raw in conn.execute("SELECT id, config FROM profiles").fetchall()}

        rebuilt = []
        for table in _TABLES:
            if table in tables and "profile_id" not in _columns(conn, table):
                _rebuild(conn, table)
                rebuilt.append(table)
        for table, kind in _FRAGMENT_MAPS:
            if table not in rebuilt:
                continue
            for pid, config in configs.items():
                switches = (config.get("fragments") or {}).get(kind) or {}
                if pid == active or not isinstance(switches, dict):
                    continue
                conn.executemany(
                    f"UPDATE {table} SET enabled = ? WHERE profile_id = ? AND id = ?",  # nosec B608 -- fixed names
                    [(1 if on else 0, pid, fid) for fid, on in switches.items()],
                )

        if "worlds" in tables:
            conn.execute(_PROFILE_WORLDS)
            if "is_global" in _columns(conn, "worlds"):
                conn.execute(
                    "INSERT OR IGNORE INTO profile_worlds (profile_id, world_id) "
                    "SELECT p.id, w.id FROM worlds w CROSS JOIN profiles p WHERE w.is_global = 1"
                )
                conn.execute("ALTER TABLE worlds DROP COLUMN is_global")

        for pid, config in configs.items():
            if "fragments" in config:
                del config["fragments"]
                conn.execute("UPDATE profiles SET config = ? WHERE id = ?", (json.dumps(config, ensure_ascii=False), pid))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        if had_fk:
            conn.execute("PRAGMA foreign_keys = ON")
    return ", ".join(rebuilt) or "nothing"


def migrate(conn: sqlite3.Connection) -> None:
    _make_profiles(conn)
    if "profiles" not in _tables(conn):
        return
    rebuilt = _split_libraries(conn)
    count = conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    print(f"[migrations] 0101: prompt layouts are profiles; each of {count} has its own library; rebuilt {rebuilt}")
