"""
0008_multi_endpoints — create endpoints and model_configs tables,
add active_endpoint_id / active_model_config_id columns to settings,
and seed one endpoint+model from the existing flat settings row.
"""
from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS endpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL,
            api_key TEXT NOT NULL DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS model_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
            model_name TEXT NOT NULL,
            system_prompt TEXT NOT NULL DEFAULT '',
            temperature REAL NOT NULL DEFAULT 0.8,
            min_p REAL NOT NULL DEFAULT 0.0,
            top_k INTEGER NOT NULL DEFAULT 40,
            top_p REAL NOT NULL DEFAULT 0.95,
            repetition_penalty REAL NOT NULL DEFAULT 1.0,
            max_tokens INTEGER NOT NULL DEFAULT 4096
        )
    """)

    settings_cols = [row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()]
    if "active_endpoint_id" not in settings_cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN active_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL"
        )
    if "active_model_config_id" not in settings_cols:
        conn.execute(
            "ALTER TABLE settings ADD COLUMN active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL"
        )

    # Seed from existing flat settings if endpoints table is still empty
    ep_count = conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0]
    if ep_count == 0:
        cursor = conn.execute("SELECT * FROM settings WHERE id = 1")
        row = cursor.fetchone()
        if row:
            cols = [d[0] for d in cursor.description]
            s = dict(zip(cols, row))
            cur_ep = conn.execute(
                "INSERT INTO endpoints (url, api_key) VALUES (?, ?)",
                (s.get("endpoint_url", "http://localhost:5000/v1"), s.get("api_key", "")),
            )
            endpoint_id = cur_ep.lastrowid
            cur_mc = conn.execute(
                "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    endpoint_id,
                    s.get("model_name", "default"),
                    s.get("system_prompt", ""),
                    s.get("temperature", 0.8),
                    s.get("min_p", 0.0),
                    s.get("top_k", 40),
                    s.get("top_p", 0.95),
                    s.get("repetition_penalty", 1.0),
                    s.get("max_tokens", 4096),
                ),
            )
            model_config_id = cur_mc.lastrowid
            conn.execute(
                "UPDATE settings SET active_endpoint_id = ?, active_model_config_id = ? WHERE id = 1",
                (endpoint_id, model_config_id),
            )
            print(f"[migrations] 0008: seeded endpoint id={endpoint_id}, model_config id={model_config_id}")
