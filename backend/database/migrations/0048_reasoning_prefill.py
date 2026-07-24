"""
0048_reasoning_prefill -- add the per-pass reasoning-prefill column: the text
mode-only seed text prepended inside each pass's thought channel. Mirrors
reasoning_enabled_passes (same three keys, same JSON-blob shape).
"""

from __future__ import annotations

import sqlite3

_DEFAULT = '{"director":"","writer":"","editor":""}'


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "reasoning_prefill_passes" not in cols:
        conn.execute(f"ALTER TABLE settings ADD COLUMN reasoning_prefill_passes TEXT NOT NULL DEFAULT '{_DEFAULT}'")
        print("[migrations] 0048: added reasoning_prefill_passes column to settings")
