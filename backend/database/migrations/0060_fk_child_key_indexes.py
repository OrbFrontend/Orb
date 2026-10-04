"""Index message and attachment foreign-key child columns.

Without these indexes, each cascade searches the child table. Combined with
deepest-first deletion, indexed lookups avoid quadratic subtree deletion.
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
        # A database can be old enough to predate the table *or* the column -- the chain runs in order, but a partially-seeded
        # upgrade fixture only has what its own era created. Either way the missing piece arrives with the migration that
        # introduces it, whose DDL comes from schema.py and already carries the index.
        if not _has_column(conn, table, column):
            continue
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})")  # nosec B608 -- module-literal identifiers
        created.append(name)
    if created:
        print(f"[migrations] 0060: ensured {len(created)} foreign-key child indexes")
