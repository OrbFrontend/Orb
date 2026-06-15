"""
0018_legacy_pre_migration_columns -- catch-all for schema changes that were
historically applied inline by ``init_db()`` before the migration system
existed (or before we started numbering them).

Each block is idempotent so it stays safe to run on a database that has been
upgraded incrementally over time. On a fresh install ``CREATE_TABLES_SQL``
already creates the latest shape, so every check below short-circuits.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    settings_cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "enable_agent" not in settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN enable_agent INTEGER NOT NULL DEFAULT 1")
    if "length_guard_max_words" not in settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN length_guard_max_words INTEGER NOT NULL DEFAULT 400")
    if "length_guard_max_paragraphs" not in settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN length_guard_max_paragraphs INTEGER NOT NULL DEFAULT 5")
    if "reasoning_enabled_passes" not in settings_cols:
        conn.execute(
            'ALTER TABLE settings ADD COLUMN reasoning_enabled_passes TEXT NOT NULL DEFAULT \'{"director":true,"writer":false,"editor":false}\''
        )
    if "active_persona_id" not in settings_cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN active_persona_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL"
        )
    if "character_library_view" not in settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN character_library_view TEXT NOT NULL DEFAULT 'grid'")
    if "character_library_sort" not in settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN character_library_sort TEXT NOT NULL DEFAULT 'time-added'")
    if "tts_enabled" not in settings_cols:
        conn.execute("ALTER TABLE settings ADD COLUMN tts_enabled INTEGER NOT NULL DEFAULT 0")
    if "inspector_open_states" not in settings_cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN inspector_open_states TEXT NOT NULL DEFAULT "
            '\'{"reasoning":true,"tool_calls":false,"injection_block":false,"context_size":true}\''
        )

    model_config_cols = {row[1] for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
    if "role" not in model_config_cols:
        conn.execute("ALTER TABLE model_configs ADD COLUMN role TEXT NOT NULL DEFAULT 'writer'")
        # All existing configs just got role='writer'. Create a fresh role='agent'
        # config for every endpoint (including those that already had
        # agent_active_model_config_id set, since that column also pointed to a
        # writer-role config before this migration).
        ep_cursor = conn.execute("SELECT * FROM endpoints")
        ep_col_names = [d[0] for d in ep_cursor.description]
        ep_rows = ep_cursor.fetchall()
        for ep_row in ep_rows:
            ep = dict(zip(ep_col_names, ep_row))
            mc_cursor = conn.execute(
                "SELECT * FROM model_configs WHERE endpoint_id = ? AND id = ?",
                (ep["id"], ep.get("active_model_config_id")),
            )
            mc_col_names = [d[0] for d in mc_cursor.description]
            mc_rows = mc_cursor.fetchall()
            if not mc_rows:
                mc_cursor = conn.execute(
                    "SELECT * FROM model_configs WHERE endpoint_id = ? LIMIT 1",
                    (ep["id"],),
                )
                mc_col_names = [d[0] for d in mc_cursor.description]
                mc_rows = mc_cursor.fetchall()
            if mc_rows:
                mc = dict(zip(mc_col_names, mc_rows[0]))
                cur = conn.execute(
                    "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, 'agent')",
                    (
                        ep["id"],
                        mc["model_name"],
                        mc["temperature"],
                        mc["min_p"],
                        mc["top_k"],
                        mc["top_p"],
                        mc["repetition_penalty"],
                        mc["max_tokens"],
                    ),
                )
            else:
                cur = conn.execute(
                    "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, 'default', '', 0.8, 0.0, 40, 0.95, 1.0, 4096, 'agent')",
                    (ep["id"],),
                )
            conn.execute(
                "UPDATE endpoints SET agent_active_model_config_id = ? WHERE id = ?",
                (cur.lastrowid, ep["id"]),
            )

    director_cols = {row[1] for row in conn.execute("PRAGMA table_info(director_state)").fetchall()}
    if "keywords" not in director_cols:
        conn.execute("ALTER TABLE director_state ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]'")

    fragment_cols = {row[1] for row in conn.execute("PRAGMA table_info(mood_fragments)").fetchall()}
    if "enabled" not in fragment_cols:
        conn.execute("ALTER TABLE mood_fragments ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")

    character_cols = {row[1] for row in conn.execute("PRAGMA table_info(character_cards)").fetchall()}
    if "world_id" not in character_cols:
        conn.execute(
            "ALTER TABLE character_cards ADD COLUMN world_id TEXT DEFAULT NULL REFERENCES worlds(id) ON DELETE SET NULL"
        )

    log_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_logs)").fetchall()}
    if "message_id" not in log_cols:
        conn.execute("ALTER TABLE conversation_logs ADD COLUMN message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL")
    for col in ("reasoning_director", "reasoning_writer", "reasoning_editor"):
        if col not in log_cols:
            conn.execute(f"ALTER TABLE conversation_logs ADD COLUMN {col} TEXT")
