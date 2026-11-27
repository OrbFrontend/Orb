"""Suggestion-miner persistence and corpus reads.

Child-process corpus readers use a synchronous read-only connection; app operations are async. Read only assistant replies after
turn 0, excluding user messages and card-authored greetings. Suggestions, dismissals and the run state belong to one profile,
since a run mines against that profile's bank; without a ``profile_id`` the app operations act on the active profile.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Iterator, Sequence
from pathlib import Path
from typing import cast

from ..connection import immediate_tx, select_rows
from ..models import SlopCardRow, SlopMiningStateRow, SlopReplyRow, SlopSuggestionDraft, SlopSuggestionRow
from .profiles import get_active_profile_id

_MODEL_REPLIES = "m.role = 'assistant' AND m.turn_index > 0"

# A group reply belongs to the member who spoke it; anything else to the conversation's character. Each fallback is prefixed
# with its kind so a card id can never equal a name.
_CHARACTER_KEY = """
    CASE WHEN gm.id IS NOT NULL THEN
        COALESCE('card:' || NULLIF(gm.character_card_id, ''), 'name:' || lower(trim(gm.display_name)))
    ELSE
        COALESCE('card:' || NULLIF(c.character_card_id, ''),
                 'name:' || NULLIF(lower(trim(c.character_name)), ''),
                 'conv:' || c.id)
    END
"""


# -- corpus reads (child process, read-only) ---------------------------------


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


# -- app surface -------------------------------------------------------------


def _suggestion(row) -> SlopSuggestionRow:
    out = dict(row)
    for column in ("stats", "fillers", "examples"):
        out[column] = json.loads(out[column])
    return cast(SlopSuggestionRow, out)


async def get_slop_mining_state(profile_id: str | None = None) -> SlopMiningStateRow | None:
    """When the miner last ran and how that run ended, or ``None`` if it never has."""
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows("SELECT last_run_at, last_status FROM slop_mining_state WHERE profile_id = ?", (profile_id,))
    return cast(SlopMiningStateRow, dict(rows[0])) if rows else None


async def list_slop_suggestions(profile_id: str | None = None) -> list[SlopSuggestionRow]:
    """Stored suggestions in rank order: each run inserts its picks best first."""
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows(
        "SELECT id, key, lane, label, pattern, stats, fillers, examples, mined_at FROM slop_suggestions "
        "WHERE profile_id = ? ORDER BY id",
        (profile_id,),
    )
    return [_suggestion(row) for row in rows]


async def list_slop_suggestion_keys(profile_id: str | None = None) -> list[str]:
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows("SELECT key FROM slop_suggestions WHERE profile_id = ?", (profile_id,))
    return [str(row[0]) for row in rows]


async def list_slop_dismissals(profile_id: str | None = None) -> tuple[list[str], list[str]]:
    """Dismissed keys and their patterns; a run offers neither again."""
    profile_id = profile_id or await get_active_profile_id()
    rows = await select_rows("SELECT key, pattern FROM slop_dismissals WHERE profile_id = ? ORDER BY key", (profile_id,))
    return [str(row[0]) for row in rows], [str(row[1]) for row in rows]


async def replace_slop_suggestions(
    drafts: Sequence[SlopSuggestionDraft] | None,
    *,
    status: str,
    mined_at: str,
    profile_id: str,
    keys_at_start: Collection[str] = (),
) -> None:
    """Record a run for *profile_id* and atomically replace its suggestions; None preserves existing results. Nothing is
    recorded when the profile was deleted mid-run.

    Do not reoffer keys removed since keys_at_start. Re-read dismissals and bank
    regexes under the write lock to preserve actions taken during mining; a
    dismissed pattern stays gone even when a different key mines it.
    """
    async with immediate_tx() as db:
        if not list(await db.execute_fetchall("SELECT 1 FROM profiles WHERE id = ?", (profile_id,))):
            return
        if drafts is not None:
            current = {
                str(row[0])
                for row in await db.execute_fetchall("SELECT key FROM slop_suggestions WHERE profile_id = ?", (profile_id,))
            }
            skip = set(keys_at_start) - current
            dismissed = list(
                await db.execute_fetchall("SELECT key, pattern FROM slop_dismissals WHERE profile_id = ?", (profile_id,))
            )
            skip |= {str(row[0]) for row in dismissed}
            hidden = {row[1] for row in dismissed}
            hidden |= {
                row[0]
                for row in await db.execute_fetchall(
                    "SELECT pattern FROM phrase_bank WHERE profile_id = ? AND kind = 'regex'", (profile_id,)
                )
            }
            await db.execute("DELETE FROM slop_suggestions WHERE profile_id = ?", (profile_id,))
            await db.executemany(
                "INSERT INTO slop_suggestions (profile_id, key, lane, label, pattern, stats, fillers, examples, mined_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        profile_id,
                        d["key"],
                        d["lane"],
                        d["label"],
                        d["pattern"],
                        *map(json.dumps, (d["stats"], d["fillers"], d["examples"])),
                        mined_at,
                    )
                    for d in drafts
                    if d["key"] not in skip and d["pattern"] not in hidden
                ],
            )
        await db.execute(
            "INSERT INTO slop_mining_state (profile_id, last_run_at, last_status) VALUES (?, ?, ?) "
            "ON CONFLICT(profile_id) DO UPDATE SET last_run_at = excluded.last_run_at, last_status = excluded.last_status",
            (profile_id, mined_at, status),
        )


async def accept_slop_suggestion(suggestion_id: int, pattern: str, profile_id: str | None = None) -> int | None:
    """Create a regex phrase group from a suggestion and drop the suggestion.

    The caller has validated *pattern*. Returns the new group's id, or ``None`` when the suggestion no longer exists (a run
    replaced it, or it was handled in another tab) -- in which case nothing is written.
    """
    profile_id = profile_id or await get_active_profile_id()
    async with immediate_tx() as db:
        cursor = await db.execute("DELETE FROM slop_suggestions WHERE id = ? AND profile_id = ?", (suggestion_id, profile_id))
        if cursor.rowcount == 0:
            return None
        cursor = await db.execute(
            "INSERT INTO phrase_bank (profile_id, variants, kind, pattern) VALUES (?, '[]', 'regex', ?)", (profile_id, pattern)
        )
        assert cursor.lastrowid is not None
        return cursor.lastrowid


async def dismiss_slop_suggestion(suggestion_id: int, dismissed_at: str, profile_id: str | None = None) -> bool:
    """Remember a suggestion's key as dismissed and drop the suggestion."""
    profile_id = profile_id or await get_active_profile_id()
    async with immediate_tx() as db:
        rows = list(
            await db.execute_fetchall(
                "SELECT key, pattern FROM slop_suggestions WHERE id = ? AND profile_id = ?", (suggestion_id, profile_id)
            )
        )
        if not rows:
            return False
        await db.execute(
            "INSERT INTO slop_dismissals (profile_id, key, pattern, dismissed_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(profile_id, key) DO UPDATE SET pattern = excluded.pattern, dismissed_at = excluded.dismissed_at",
            (profile_id, rows[0]["key"], rows[0]["pattern"], dismissed_at),
        )
        await db.execute("DELETE FROM slop_suggestions WHERE id = ?", (suggestion_id,))
    return True
