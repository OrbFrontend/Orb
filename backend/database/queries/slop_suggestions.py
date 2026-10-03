"""Database operations for the Phrase Bank suggestion miner.

Two halves. The corpus reads are synchronous and take a plain ``sqlite3``
connection, because a run happens in a child process that opens the database
read-only (:func:`open_readonly`). The rest is the app's async surface: stored
suggestions, dismissals, staleness bookkeeping, and the accept path, which is
the only place a suggestion becomes a phrase-bank entry.

User messages are never read: every reply query filters ``role = 'assistant'``,
and ``turn_index > 0`` leaves out turn 0, which holds card-authored greetings.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path
from typing import cast

from ..connection import get_db, immediate_tx
from ..models import SlopCardRow, SlopReplyRow, SlopSuggestionDraft, SlopSuggestionRow

_MODEL_REPLIES = "m.role = 'assistant' AND m.turn_index > 0"

# A group reply belongs to the member who spoke it; anything else to the
# conversation's character. Each fallback is prefixed with its kind so a card id
# can never equal a name.
_CHARACTER_KEY = """
    CASE WHEN gm.id IS NOT NULL THEN
        COALESCE('card:' || NULLIF(gm.character_card_id, ''), 'name:' || lower(trim(gm.display_name)))
    ELSE
        COALESCE('card:' || NULLIF(c.character_card_id, ''),
                 'name:' || NULLIF(lower(trim(c.character_name)), ''),
                 'conv:' || c.id)
    END
"""


# ── corpus reads (child process, read-only) ─────────────────────────────────


def open_readonly(db_path: str) -> sqlite3.Connection:
    """Open *db_path* read-only; a run can never write the live database."""
    conn = sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def iter_model_replies(conn: sqlite3.Connection) -> Iterator[SlopReplyRow]:
    """Every model reply, every swipe included, grouped by character key."""
    cursor = conn.execute(
        f"SELECT {_CHARACTER_KEY} AS character_key, m.created_at, m.content "  # nosec B608 -- constant SQL fragments
        "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
        f"LEFT JOIN group_members gm ON gm.id = m.speaker_member_id WHERE {_MODEL_REPLIES} "
        "ORDER BY character_key, m.id"
    )
    for row in cursor:
        yield cast(SlopReplyRow, dict(row))


def read_card_rows(conn: sqlite3.Connection) -> list[SlopCardRow]:
    """Every card's authored prose and profile prose."""
    cards: list[SlopCardRow] = []
    for row in conn.execute(
        "SELECT name, description, personality, scenario, first_mes, alternate_greetings, mes_example "
        "FROM character_cards ORDER BY id"
    ):
        card = dict(row)
        try:
            greetings = json.loads(card["alternate_greetings"] or "[]")
        except json.JSONDecodeError:
            greetings = []
        card["alternate_greetings"] = greetings if isinstance(greetings, list) else []
        cards.append(cast(SlopCardRow, card))
    return cards


def read_names(conn: sqlite3.Connection) -> list[str]:
    """Conversation character names, persona names, and group-member display names."""
    rows = conn.execute(
        "SELECT character_name FROM conversations UNION SELECT name FROM user_personas "
        "UNION SELECT display_name FROM group_members UNION SELECT user_name FROM settings"
    )
    return [str(row[0]) for row in rows if row[0]]


# ── app surface ─────────────────────────────────────────────────────────────


def _suggestion(row) -> SlopSuggestionRow:
    out = dict(row)
    for column in ("stats", "fillers", "examples"):
        out[column] = json.loads(out[column])
    return cast(SlopSuggestionRow, out)


async def count_model_replies() -> int:
    async with get_db() as db:
        rows = list(await db.execute_fetchall(f"SELECT COUNT(*) FROM messages m WHERE {_MODEL_REPLIES}"))  # nosec B608
    return int(rows[0][0])


async def get_slop_replies_at_run() -> int | None:
    """The model reply count when the miner last ran, or ``None`` if it never has."""
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT replies_at_run FROM slop_mining_state WHERE id = 1"))
    return int(rows[0][0]) if rows else None


async def list_slop_suggestions() -> list[SlopSuggestionRow]:
    """Stored suggestions in rank order: each run inserts its picks best first."""
    async with get_db() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT id, key, lane, label, pattern, stats, fillers, examples, mined_at FROM slop_suggestions ORDER BY id"
            )
        )
    return [_suggestion(row) for row in rows]


async def list_slop_suggestion_keys() -> list[str]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT key FROM slop_suggestions"))
    return [str(row[0]) for row in rows]


async def list_slop_dismissed_keys() -> list[str]:
    async with get_db() as db:
        rows = list(await db.execute_fetchall("SELECT key FROM slop_dismissals ORDER BY key"))
    return [str(row[0]) for row in rows]


async def replace_slop_suggestions(
    drafts: Sequence[SlopSuggestionDraft] | None,
    *,
    replies_at_run: int,
    status: str,
    mined_at: str,
    keys_at_start: Collection[str] = (),
) -> None:
    """Record a run and replace every stored suggestion with its results, in one
    transaction. ``None`` (a skipped or failed run) keeps the stored suggestions.

    A run takes a minute of wall clock, and the user may accept or dismiss a
    suggestion meanwhile. Only those two actions (and a reset) remove single
    rows, so a key in *keys_at_start* that is gone now was handled mid-run and
    is not offered back, even if it was accepted with an edited pattern.
    Dismissals and regex bank entries are re-read under the write lock too.
    """
    async with immediate_tx() as db:
        if drafts is not None:
            current = {str(row[0]) for row in await db.execute_fetchall("SELECT key FROM slop_suggestions")}
            skip = set(keys_at_start) - current
            skip |= {str(row[0]) for row in await db.execute_fetchall("SELECT key FROM slop_dismissals")}
            banked = {row[0] for row in await db.execute_fetchall("SELECT pattern FROM phrase_bank WHERE kind = 'regex'")}
            await db.execute("DELETE FROM slop_suggestions")
            await db.executemany(
                "INSERT INTO slop_suggestions (key, lane, label, pattern, stats, fillers, examples, mined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        d["key"],
                        d["lane"],
                        d["label"],
                        d["pattern"],
                        *map(json.dumps, (d["stats"], d["fillers"], d["examples"])),
                        mined_at,
                    )
                    for d in drafts
                    if d["key"] not in skip and d["pattern"] not in banked
                ],
            )
        await db.execute(
            "INSERT INTO slop_mining_state (id, last_run_at, replies_at_run, last_status) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET last_run_at = excluded.last_run_at, "
            "replies_at_run = excluded.replies_at_run, last_status = excluded.last_status",
            (mined_at, replies_at_run, status),
        )


async def accept_slop_suggestion(suggestion_id: int, pattern: str) -> int | None:
    """Create a regex phrase group from a suggestion and drop the suggestion.

    The caller has validated *pattern*. Returns the new group's id, or ``None``
    when the suggestion no longer exists (a run replaced it, or it was handled
    in another tab) -- in which case nothing is written.
    """
    async with immediate_tx() as db:
        cursor = await db.execute("DELETE FROM slop_suggestions WHERE id = ?", (suggestion_id,))
        if cursor.rowcount == 0:
            return None
        cursor = await db.execute(
            "INSERT INTO phrase_bank (variants, kind, pattern) VALUES ('[]', 'regex', ?)",
            (pattern,),
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


async def dismiss_slop_suggestion(suggestion_id: int, dismissed_at: str) -> bool:
    """Remember a suggestion's key as dismissed and drop the suggestion."""
    async with immediate_tx() as db:
        rows = list(await db.execute_fetchall("SELECT key, pattern FROM slop_suggestions WHERE id = ?", (suggestion_id,)))
        if not rows:
            return False
        await db.execute(
            "INSERT INTO slop_dismissals (key, pattern, dismissed_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET pattern = excluded.pattern, dismissed_at = excluded.dismissed_at",
            (rows[0]["key"], rows[0]["pattern"], dismissed_at),
        )
        await db.execute("DELETE FROM slop_suggestions WHERE id = ?", (suggestion_id,))
    return True
