"""Drop the connection, model and sampler columns from ``settings``.

The active endpoint row owns ``url`` and ``api_key``, and its active Writer model config owns the model name and samplers.
``get_settings`` reads them from there and fills fixed values when nothing is selected, so no code reads these columns. Old
presets and backups pass through this migration on import. A rerun finds nothing left to drop and changes nothing.
"""

from __future__ import annotations

import sqlite3

_COLUMNS: tuple[str, ...] = (
    "endpoint_url",
    "api_key",
    "model_name",
    "temperature",
    "min_p",
    "top_k",
    "top_p",
    "repetition_penalty",
    "max_tokens",
)


def migrate(conn: sqlite3.Connection) -> None:
    present = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    dropped = [column for column in _COLUMNS if column in present]
    for column in dropped:
        conn.execute(f"ALTER TABLE settings DROP COLUMN {column}")  # nosec B608 -- module literals
    conn.commit()
    if dropped:
        print(f"[migrations] 0080: dropped {', '.join(dropped)} from settings")
