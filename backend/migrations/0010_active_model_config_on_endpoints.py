"""
0010_active_model_config_on_endpoints — move active_model_config_id ownership
from settings to endpoints so each endpoint independently remembers its last-
used model config.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    endpoint_cols = {
        row[1] for row in conn.execute("PRAGMA table_info(endpoints)").fetchall()
    }
    if "active_model_config_id" not in endpoint_cols:
        conn.execute(
            "ALTER TABLE endpoints ADD COLUMN active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL"
        )

    # Backfill: if settings has an active_endpoint_id and active_model_config_id,
    # and the model config belongs to that endpoint, copy it over.
    row = conn.execute(
        "SELECT active_endpoint_id, active_model_config_id FROM settings WHERE id = 1"
    ).fetchone()
    if row:
        active_ep_id, active_mc_id = row
        if active_ep_id and active_mc_id:
            mc = conn.execute(
                "SELECT id FROM model_configs WHERE id = ? AND endpoint_id = ?",
                (active_mc_id, active_ep_id),
            ).fetchone()
            if mc:
                conn.execute(
                    "UPDATE endpoints SET active_model_config_id = ? WHERE id = ?",
                    (active_mc_id, active_ep_id),
                )
                print(
                    f"[migrations] 0010: backfilled endpoint {active_ep_id} "
                    f"active_model_config_id={active_mc_id}"
                )
