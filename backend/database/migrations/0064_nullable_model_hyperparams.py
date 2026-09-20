"""Allow a model config to omit individual provider request parameters."""

import sqlite3

_HYPERPARAMS = ("temperature", "min_p", "top_k", "top_p", "repetition_penalty", "max_tokens")
_COLUMNS = (
    "id, endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, "
    "repetition_penalty, max_tokens, role, reasoning_effort, reasoning_effort_param, "
    "reasoning_effort_value, extra_headers, extra_body"
)


def migrate(conn: sqlite3.Connection) -> None:
    """Rebuild SQLite's table to remove NOT NULL from sampler columns.

    SQLite cannot drop a NOT NULL constraint in place.  IDs are copied verbatim,
    preserving endpoint active-model references; no model_configs indexes exist.
    """
    info = {row[1]: row for row in conn.execute("PRAGMA table_info(model_configs)").fetchall()}
    if not any(info.get(name, (None, None, None, 0))[3] for name in _HYPERPARAMS):
        return

    foreign_keys = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute(
            """
            CREATE TABLE model_configs_nullable (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
                model_name TEXT NOT NULL,
                system_prompt TEXT NOT NULL DEFAULT '',
                temperature REAL DEFAULT 0.8,
                min_p REAL DEFAULT 0.0,
                top_k INTEGER DEFAULT 40,
                top_p REAL DEFAULT 0.95,
                repetition_penalty REAL DEFAULT 1.0,
                max_tokens INTEGER DEFAULT 4096,
                role TEXT NOT NULL DEFAULT 'writer' CHECK (role IN ('writer', 'agent')),
                reasoning_effort TEXT NOT NULL DEFAULT '',
                reasoning_effort_param TEXT NOT NULL DEFAULT '',
                reasoning_effort_value TEXT NOT NULL DEFAULT '',
                extra_headers TEXT NOT NULL DEFAULT '',
                extra_body TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(f"INSERT INTO model_configs_nullable ({_COLUMNS}) SELECT {_COLUMNS} FROM model_configs")
        conn.execute("DROP TABLE model_configs")
        conn.execute("ALTER TABLE model_configs_nullable RENAME TO model_configs")
    finally:
        conn.execute(f"PRAGMA foreign_keys = {foreign_keys}")
    print("[migrations] 0064: made model config hyperparameters nullable")
