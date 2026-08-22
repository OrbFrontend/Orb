"""
0054_group_chats -- group scenes: durable rosters, per-speaker identity, and the
scene-local sheet review queue.

Takes a pre-group-chats database to the shape ``schema.py`` ships, in one step.
Group chat was built over several migrations, none of which ever reached an
installed database, so their intermediate shapes are not history anybody has to
replay. What follows is the end state only -- notably ``messages.exchange_id``,
which spent part of that development called ``beat_id`` and is created here
under its final name. (What the Director tells a single speaker to do, which
also carried the name for a while, is now a *cue* -- see ``pipeline/cast.py``. An
*exchange* is one group request, which is what this column groups.)

The ``conversations`` columns are the per-scene policy: ``kind`` splits a group
from a solo chat, ``group_turn_mode``/``group_max_speakers`` decide who speaks,
``group_context_mode`` decides what each speaker sees, and
``group_sheet_updates`` is the opt-in to the post-exchange sheet pass -- off for
every existing scene, which is the honest default, since the pass costs one
billed call per member an exchange touched. ``group_root_id`` names the
conversation a family descends from, so a fork stops reading as a new group in
the sidebar; NULL means the row *is* that root, which is why nothing is
backfilled here: every conversation that exists at migration time is the origin
of its own family, and NULL already says exactly that.

``group_members`` carries two scene-local overrides that never write the card --
``public_profile_override`` (what the rest of the cast sees) and
``card_sheet_override`` (what the member reads about itself). NULL means "fall
back to the card" for both, so there is nothing to backfill and the original is
always recoverable by clearing the override.

``member_sheet_proposals`` is where the post-exchange pass parks a proposed
rewrite of one such sheet, pending, for the user to apply or reject. The pass
does not apply its own proposal, for the two reasons Dynamic Worlds stages its
changesets: it writes a field the user can also hand-edit, and a bookkeeping
model can simply judge wrong. ``base_sheet`` is to a proposal what
``worlds.content_revision`` is to a changeset -- the apply re-reads the member's
current sheet and refuses when it has moved since the proposal was derived,
rather than clobbering whichever writer got there second. Nothing is backfilled:
no exchange has run under the pass, so no existing scene can have proposed
anything.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence

from backend.database import schema

_CONVERSATION_COLUMNS = (
    ("kind", "TEXT NOT NULL DEFAULT 'solo' CHECK (kind IN ('solo', 'group'))"),
    (
        "group_turn_mode",
        "TEXT NOT NULL DEFAULT 'director' CHECK (group_turn_mode IN ('manual', 'round_robin', 'director'))",
    ),
    ("group_max_speakers", "INTEGER NOT NULL DEFAULT 3 CHECK (group_max_speakers BETWEEN 1 AND 8)"),
    (
        "group_context_mode",
        "TEXT NOT NULL DEFAULT 'private' CHECK (group_context_mode IN ('private', 'shared', 'swap'))",
    ),
    ("group_sheet_updates", "INTEGER NOT NULL DEFAULT 0 CHECK (group_sheet_updates IN (0, 1))"),
    ("group_root_id", "TEXT DEFAULT NULL REFERENCES conversations(id) ON DELETE SET NULL"),
)

# Added after group_members exists: speaker_member_id points at it.
_MESSAGE_COLUMNS = (
    ("speaker_member_id", "TEXT DEFAULT NULL REFERENCES group_members(id) ON DELETE SET NULL"),
    ("exchange_id", "TEXT DEFAULT NULL"),
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_conversations_group_root ON conversations(group_root_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_group_member_active_card "
    "ON group_members(conversation_id, character_card_id) "
    "WHERE active = 1 AND character_card_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_messages_exchange ON messages(conversation_id, exchange_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_speaker ON messages(speaker_member_id)",
    "CREATE INDEX IF NOT EXISTS idx_sheet_proposal_conv_status ON member_sheet_proposals(conversation_id, status)",
)


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 — literal table name


def _add_columns(conn: sqlite3.Connection, table: str, additions: Sequence[tuple[str, str]]) -> None:
    existing = _columns(conn, table)
    for name, ddl in additions:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")  # nosec B608 — names from a module constant
            print(f"[migrations] 0054: added {table}.{name}")


def migrate(conn: sqlite3.Connection) -> None:
    _add_columns(conn, "conversations", _CONVERSATION_COLUMNS)

    # Both tables come from the canonical fresh-install DDL rather than a pasted
    # copy: group_members carries a UNIQUE constraint and two CHECKs,
    # member_sheet_proposals a status CHECK and two cascading edges.
    conn.execute(schema.table_create_sql("group_members"))
    conn.execute(schema.table_create_sql("member_sheet_proposals"))

    _add_columns(conn, "messages", _MESSAGE_COLUMNS)

    for sql in _INDEXES:
        conn.execute(sql)
