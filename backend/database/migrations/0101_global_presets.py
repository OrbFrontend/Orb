"""Turn prompt layouts into global presets: each preset owns its prompt layout's blocks plus a generation config.

Renames prompt_layouts to presets and settings.active_prompt_layout_id to active_preset_id, and adds the config JSON and the
lane foreign keys (writer, agent and Judge endpoints; writer and agent model configs). Every preset is filled from the live
state, so switching between former layouts changes only the blocks. 0097 added the active layout setting as NULL and only a
layout switch filled it, so a NULL or stale active id is stored as the preset it read as; the frontend reads the column
itself. A rerun changes nothing.
"""

from __future__ import annotations

import json
import sqlite3

# A snapshot of the preset shape at this migration; later changes do not rewrite existing rows.
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


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- fixed table names


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


def _switches(conn: sqlite3.Connection, tables: set[str], table: str) -> dict:
    if table not in tables:
        return {}
    return dict(conn.execute(f"SELECT id, enabled FROM {table}").fetchall())  # nosec B608 -- fixed table names


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
        "fragments": {
            "mood": _switches(conn, tables, "mood_fragments"),
            "interactive": _switches(conn, tables, "interactive_fragments"),
        },
    }
    return config, [_existing(conn, parent, value) for (_, parent), value in zip(_LANE_COLUMNS, lanes, strict=True)]


def migrate(conn: sqlite3.Connection) -> None:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
    if "prompt_layouts" in tables and "presets" not in tables:
        # Off, the rename rewrites the prompt_blocks foreign key to name the new table; some Python builds default it on.
        conn.execute("PRAGMA legacy_alter_table = OFF")
        conn.execute("ALTER TABLE prompt_layouts RENAME TO presets")
        (blocks_sql,) = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'prompt_blocks'").fetchone()
        assert "prompt_layouts" not in blocks_sql, "prompt_blocks still references prompt_layouts after the rename"
        tables = (tables - {"prompt_layouts"}) | {"presets"}
    if "presets" not in tables:
        return
    have = _columns(conn, "presets")
    if "config" not in have:
        conn.execute("ALTER TABLE presets ADD COLUMN config TEXT NOT NULL DEFAULT '{}'")
    for column, parent in _LANE_COLUMNS:
        if column not in have:
            conn.execute(
                f"ALTER TABLE presets ADD COLUMN {column} INTEGER REFERENCES {parent}(id) ON DELETE SET NULL"  # nosec B608
            )
    settings = _columns(conn, "settings") if "settings" in tables else set()
    if "active_prompt_layout_id" in settings and "active_preset_id" in settings:
        # An earlier settings rebuild used today's columns, so both are here.
        conn.execute("UPDATE settings SET active_preset_id = COALESCE(active_preset_id, active_prompt_layout_id)")
        conn.execute("ALTER TABLE settings DROP COLUMN active_prompt_layout_id")
    elif "active_prompt_layout_id" in settings:
        conn.execute("ALTER TABLE settings RENAME COLUMN active_prompt_layout_id TO active_preset_id")
    if "settings" in tables:
        # The order get_active_preset_id falls back on: 'default' first, then by name.
        conn.execute(
            """UPDATE settings SET active_preset_id = (
                   SELECT id FROM presets ORDER BY id != 'default', name COLLATE NOCASE LIMIT 1
               )
               WHERE id = 1 AND EXISTS (SELECT 1 FROM presets)
                 AND (active_preset_id IS NULL OR active_preset_id NOT IN (SELECT id FROM presets))"""
        )
    config, lanes = _live_state(conn, tables)
    filled = conn.execute(
        f"UPDATE presets SET config = ?, {', '.join(f'{c} = ?' for c, _ in _LANE_COLUMNS)} WHERE config = '{{}}'",  # nosec B608
        (json.dumps(config, ensure_ascii=False), *lanes),
    )
    conn.commit()
    print(f"[migrations] 0101: prompt layouts are presets; {filled.rowcount} filled from the live settings")
