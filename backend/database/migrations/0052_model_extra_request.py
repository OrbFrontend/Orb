"""
0052_model_extra_request -- add the per-model arbitrary request additions.

``extra_headers`` holds ``Name: value`` lines merged into the outbound HTTP
headers of every request, on both the chat and text-completion transports;
``extra_body`` holds a JSON object merged into the chat-completions request
body only. Both default to '' (send nothing), so an existing model config keeps
behaving exactly as before.

They exist so a provider knob Orb has no setting for -- a routing header like
``X-Provider``, a body field a gateway added last week -- can be driven without
a code change. This generalises ``reasoning_effort_param`` /
``reasoning_effort_value``, which do the same thing but only for reasoning and
only into the body. Validation stays at the API layer, not in the DB.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
    for col in ("extra_headers", "extra_body"):
        if col not in cols:
            conn.execute(f"ALTER TABLE model_configs ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
            print(f"[migrations] 0052: added {col} column to model_configs")
