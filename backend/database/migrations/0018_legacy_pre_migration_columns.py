"""Backfill schema changes formerly applied inline by init_db.

Each block is idempotent for incrementally upgraded databases.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    add_columns(
        conn,
        "settings",
        "enable_agent INTEGER NOT NULL DEFAULT 1",
        "length_guard_max_words INTEGER NOT NULL DEFAULT 400",
        "length_guard_max_paragraphs INTEGER NOT NULL DEFAULT 5",
        'reasoning_enabled_passes TEXT NOT NULL DEFAULT \'{"director":true,"writer":false,"editor":false}\'',
        "active_persona_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL",
        "character_library_view TEXT NOT NULL DEFAULT 'grid'",
        "character_library_sort TEXT NOT NULL DEFAULT 'time-added'",
        "tts_enabled INTEGER NOT NULL DEFAULT 0",
        'inspector_open_states TEXT NOT NULL DEFAULT \'{"reasoning":true,"tool_calls":false,"injection_block":false,"context_size":true}\'',
    )

    model_config_cols = {row[1] for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
    if "role" not in model_config_cols:
        conn.execute("ALTER TABLE model_configs ADD COLUMN role TEXT NOT NULL DEFAULT 'writer'")
        # All existing configs just got role='writer'. Create a fresh role='agent' config for every endpoint (including those
        # that already had agent_active_model_config_id set, since that column also pointed to a writer-role config before this
        # migration).
        ep_cursor = conn.execute("SELECT * FROM endpoints")
        ep_col_names = [d[0] for d in ep_cursor.description]
        ep_rows = ep_cursor.fetchall()
        for ep_row in ep_rows:
            ep = dict(zip(ep_col_names, ep_row))
            mc_cursor = conn.execute(
                "SELECT * FROM model_configs WHERE endpoint_id = ? AND id = ?", (ep["id"], ep.get("active_model_config_id"))
            )
            mc_col_names = [d[0] for d in mc_cursor.description]
            mc_rows = mc_cursor.fetchall()
            if not mc_rows:
                mc_cursor = conn.execute("SELECT * FROM model_configs WHERE endpoint_id = ? LIMIT 1", (ep["id"],))
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
            conn.execute("UPDATE endpoints SET agent_active_model_config_id = ? WHERE id = ?", (cur.lastrowid, ep["id"]))

    add_columns(conn, "director_state", "keywords TEXT NOT NULL DEFAULT '[]'")

    add_columns(conn, "mood_fragments", "enabled BOOLEAN NOT NULL DEFAULT 1")

    add_columns(conn, "character_cards", "world_id TEXT DEFAULT NULL REFERENCES worlds(id) ON DELETE SET NULL")

    log_cols = {row[1] for row in conn.execute("PRAGMA table_info(conversation_logs)").fetchall()}
    add_columns(conn, "conversation_logs", "message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL")
    for col in ("reasoning_director", "reasoning_writer", "reasoning_editor"):
        if col not in log_cols:
            conn.execute(f"ALTER TABLE conversation_logs ADD COLUMN {col} TEXT")
