"""
0046_character_card_extensions -- persist the V2 card `extensions` dict.

Previously parsed on import but silently dropped. Stored as JSON so
third-party extension data round-trips through export, and Orb's own
card-embedded fragments live at extensions.orb.fragments.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(character_cards)").fetchall()}
    if "extensions" not in cols:
        conn.execute("ALTER TABLE character_cards ADD COLUMN extensions TEXT DEFAULT NULL")
        print("[migrations] 0046: added extensions column to character_cards")
