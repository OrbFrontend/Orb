"""Keep the tags a card was imported with, so a run is recoverable.

Superseded by 0061, which drops this column: the two-list design below lost to a
single visible tag list plus a confirmation in front of the run. Kept in the
chain because it ran, and because a database upgrading through it still needs
the column to exist before 0061 can drop it.

0059 made the tagger rewrite ``character_cards.tags`` — the same column the card
editor shows and, crucially, the one ``to_png`` writes into an exported card's
V2/V3 chunk. That made a run destroy creator metadata in a file the user may go
on to share, with no way back.

This column is the way back. ``apply_auto_tags`` fills it once, on the first run
that touches a card, and never again: it is what the card came with, not a
running backup of the previous answer. ``''`` means untouched by any run, which
is what distinguishes it from ``'[]'`` — a card that genuinely arrived untagged.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_cards'").fetchone() is None:
        return
    if "imported_tags" in {row[1] for row in conn.execute("PRAGMA table_info(character_cards)").fetchall()}:
        return
    conn.execute("ALTER TABLE character_cards ADD COLUMN imported_tags TEXT NOT NULL DEFAULT ''")
    # Cards already tagged by 0058/0059 lost their originals before this column
    # existed; nothing to backfill, and '' correctly reports that.
    print("[migrations] 0060: added imported_tags column to character_cards")
