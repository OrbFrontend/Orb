"""0013_agent_endpoint -- add separate agent endpoint/model configuration columns for databases created before this feature
existed.
"""

from __future__ import annotations

import sqlite3

from .helpers import add_columns


def migrate(conn: sqlite3.Connection) -> None:
    add_columns(
        conn,
        "settings",
        "agent_same_as_writer INTEGER NOT NULL DEFAULT 1",
        "agent_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL",
        "agent_shared_system_prompt TEXT NOT NULL DEFAULT ''",
        migration="0013",
    )

    add_columns(
        conn,
        "endpoints",
        "agent_active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL",
        migration="0013",
    )
