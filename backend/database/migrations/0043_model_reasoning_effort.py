"""
0043_model_reasoning_effort -- add the per-model reasoning effort columns.

``reasoning_effort`` holds an OpenAI-style level (``none``/``minimal``/``low``/
``medium``/``high``/``xhigh``), the sentinel ``custom``, or '' (the default),
meaning no effort param is sent and the provider's own default governs.
``reasoning_effort_param`` / ``reasoning_effort_value`` are used only with
``custom``: the exact body key and value to send, so a provider whose reasoning
control Orb doesn't know yet can be driven without a code change. The value is
JSON-decoded when it parses (numbers, objects) and sent as a string otherwise;
validation stays at the API layer, not in the DB.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
    for col in ("reasoning_effort", "reasoning_effort_param", "reasoning_effort_value"):
        if col not in cols:
            conn.execute(f"ALTER TABLE model_configs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            print(f"[migrations] 0043: added {col} column to model_configs")
