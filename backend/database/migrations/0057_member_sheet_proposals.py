"""0057_member_sheet_proposals -- the review queue for scene-local sheet updates.

``0056`` gave a member a scene-local sheet it can read about itself. This is
where the post-beat pass parks a proposed rewrite of one, pending, for the user
to apply or reject.

A proposal is not applied by the pass that wrote it, for the two reasons Dynamic
Worlds stages its changesets: the pass writes a field the user can also hand-edit,
and a bookkeeping model can simply judge wrong. ``base_sheet`` is the staleness
check the revision counter is for a World -- the apply re-reads the member's
current sheet and refuses when it has moved since the proposal was derived,
rather than silently clobbering whichever writer got there second.

Nothing is backfilled: no beat has run under the new pass, so there is nothing
for an existing scene to have proposed.
"""

from __future__ import annotations

import sqlite3

from backend.database import schema


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 — literal table name


def migrate(conn: sqlite3.Connection) -> None:
    # The opt-in. Off for every existing scene, which is the honest default: the
    # pass is one billed call per member a beat touched, and staleness is a
    # property of a long scene rather than of group chat as such.
    if "group_sheet_updates" not in _columns(conn, "conversations"):
        conn.execute(
            "ALTER TABLE conversations ADD COLUMN group_sheet_updates INTEGER NOT NULL DEFAULT 0 "
            "CHECK (group_sheet_updates IN (0, 1))"
        )
        print("[migrations] 0057: added group_sheet_updates column to conversations")

    if "member_sheet_proposals" not in _tables(conn):
        conn.execute(schema.table_create_sql("member_sheet_proposals"))
        print("[migrations] 0057: created member_sheet_proposals")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_sheet_proposal_conv_status ON member_sheet_proposals(conversation_id, status)")
