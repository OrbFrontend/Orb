"""Index every foreign-key child column that points at ``messages`` (and the two
self-references inside ``workflow_attachments``).

SQLite enforces ``ON DELETE CASCADE`` / ``ON DELETE SET NULL`` by searching the
*child* table for rows referencing the row being deleted. Without an index on
the child key that search is a full table scan, repeated once per deleted row
and once per level of a cascade -- so deleting a message subtree was quadratic
in the size of the conversation and linear in the size of every attachment
table it had to scan.

``delete_message_with_descendants`` also had to stop deleting parents before
their children, which is what made the cascade recurse at all; these indexes are
the other half. Measured on a synthetic 3,000-message chat (500 turns x 5
swipes), deleting a ~2,700-message subtree with the real query, with and without
~190 MB of attachment blobs hanging off it:

                                     no attachments   with attachments
* neither .........................      6,315 ms         7,058 ms
* deepest-first delete only .......        431 ms         1,641 ms
* deepest-first + these indexes ...         63 ms           257 ms

``direction_notes(message_id)`` and
``world_changesets(source_assistant_message_id)`` were already indexed; the rest
are added here. Each is a plain secondary index, so the migration is a pure
addition -- no table rebuild, no data movement.
"""

from __future__ import annotations

import sqlite3

# (index name, table, column). Kept in sync with the CREATE INDEX statements in
# schema.py, which the schema-equivalence gate compares a migrated database to.
_INDEXES = (
    ("idx_messages_parent", "messages", "parent_id"),
    ("idx_conversations_active_leaf", "conversations", "active_leaf_id"),
    ("idx_conversation_logs_message", "conversation_logs", "message_id"),
    ("idx_user_attachments_message", "user_attachments", "message_id"),
    ("idx_workflow_attachments_message", "workflow_attachments", "message_id"),
    ("idx_workflow_attachments_parent", "workflow_attachments", "parent_attachment_id"),
    ("idx_workflow_attachments_active_sibling", "workflow_attachments", "active_sibling_id"),
    ("idx_changeset_source_user", "world_changesets", "source_user_message_id"),
)


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
        return False
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))  # nosec B608 -- module-literal table names


def migrate(conn: sqlite3.Connection) -> None:
    created = []
    for name, table, column in _INDEXES:
        # A database can be old enough to predate the table *or* the column --
        # the chain runs in order, but a partially-seeded upgrade fixture only
        # has what its own era created. Either way the missing piece arrives
        # with the migration that introduces it, whose DDL comes from schema.py
        # and already carries the index.
        if not _has_column(conn, table, column):
            continue
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})")  # nosec B608 -- module-literal identifiers
        created.append(name)
    if created:
        print(f"[migrations] 0060: ensured {len(created)} foreign-key child indexes")
