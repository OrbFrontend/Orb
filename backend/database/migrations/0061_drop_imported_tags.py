"""0061_drop_imported_tags -- retire the stash 0060 added; the card has one tag list.

0060 kept the tags a card was imported with in a second column so an auto-tag
run could be undone and an export could ship the creator's list instead of the
owner's vocabulary. Two lists behind one visible control is the wrong trade: the
library shows the tags a run wrote, so those are the card's tags, and an export
that quietly shipped different ones from what the user was looking at was its
own kind of surprise.

The run is destructive now, and says so before it starts. Dropping the column is
safe in the direction that matters -- no reader is left holding a name -- and
lossy in the other: a database that still holds pre-run originals loses them
here. That is the accepted cost of the fold, not an oversight.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='character_cards'").fetchone() is None:
        return
    if "imported_tags" not in {row[1] for row in conn.execute("PRAGMA table_info(character_cards)").fetchall()}:
        return
    conn.execute("ALTER TABLE character_cards DROP COLUMN imported_tags")
    print("[migrations] 0061: dropped character_cards.imported_tags")
