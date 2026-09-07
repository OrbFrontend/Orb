"""Fold the auto-tagger's answers into ``character_cards.tags``.

The tagger used to keep its assignments in a ``character_auto_tags`` side table
so a run could never touch a card's imported tags. Two stores for one concept
meant every reader had to merge them, so the library filter, the API payload and
the browser all carried a second tag list. A run now rewrites the card's own
``tags`` column, and imported tags are lost the first time a card is tagged —
which is the point: a card has one set of tags.

Existing assignments are carried over rather than discarded, so a library that
has already been tagged is not re-billed. Two bookkeeping columns come with them:
``auto_tag_vocab_hash`` and ``auto_tag_card_updated_at`` are the staleness rule
that used to live on the side table's rows.
"""

from __future__ import annotations

import sqlite3


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal table names


def migrate(conn: sqlite3.Connection) -> None:
    card_cols = _columns(conn, "character_cards")
    if not card_cols:
        return
    for column in ("auto_tag_vocab_hash", "auto_tag_card_updated_at"):
        if column not in card_cols:
            conn.execute(f"ALTER TABLE character_cards ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")  # nosec B608
            print(f"[migrations] 0059: added {column} column to character_cards")

    if not _columns(conn, "character_auto_tags"):
        return
    # One UPDATE ... FROM would be shorter but needs SQLite 3.33; the correlated
    # form works on every build this project supports, and the row count here is
    # the size of one user's library.
    moved = conn.execute(
        """
        UPDATE character_cards SET
            tags = (SELECT a.tags FROM character_auto_tags a WHERE a.character_card_id = character_cards.id),
            auto_tag_vocab_hash = (SELECT a.vocab_hash FROM character_auto_tags a WHERE a.character_card_id = character_cards.id),
            auto_tag_card_updated_at = (
                SELECT a.card_updated_at FROM character_auto_tags a WHERE a.character_card_id = character_cards.id
            )
        WHERE id IN (SELECT character_card_id FROM character_auto_tags)
        """
    ).rowcount
    conn.execute("DROP TABLE character_auto_tags")
    print(f"[migrations] 0059: folded {moved} auto-tag assignments into character_cards and dropped character_auto_tags")
