"""
0002_cleanup_orphaned_messages — delete messages and related rows that reference
non‑existent conversations (foreign‑key violations) and ensure foreign keys are enabled.
"""

from __future__ import annotations

import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    # Ensure foreign keys are enabled for this connection (they already are via PRAGMA,
    # but we enforce it here as well).
    conn.execute("PRAGMA foreign_keys=ON")

    # 1. Delete orphaned messages (conversation_id not in conversations)
    cur = conn.execute(
        """
        SELECT DISTINCT m.conversation_id
        FROM messages m
        LEFT JOIN conversations c ON m.conversation_id = c.id
        WHERE c.id IS NULL
    """
    )
    missing_conversations = [row[0] for row in cur.fetchall()]

    for cid in missing_conversations:
        # Delete messages referencing missing conversation
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (cid,))
        # Delete director_state rows (if any)
        conn.execute("DELETE FROM director_state WHERE conversation_id = ?", (cid,))
        # Delete conversation_logs rows (if any)
        conn.execute("DELETE FROM conversation_logs WHERE conversation_id = ?", (cid,))
        print(f"[migrations] 0002: cleaned up orphaned rows for conversation {cid}")

    # 2. Delete orphaned message_attachments (message_id not in messages)
    conn.execute(
        """
        DELETE FROM message_attachments
        WHERE message_id NOT IN (SELECT id FROM messages)
    """
    )
    deleted = conn.total_changes
    if deleted:
        print(f"[migrations] 0002: deleted {deleted} orphaned message attachments")

    # 3. Delete orphaned director_state rows (conversation_id not in conversations)
    conn.execute(
        """
        DELETE FROM director_state
        WHERE conversation_id NOT IN (SELECT id FROM conversations)
    """
    )
    deleted = conn.total_changes
    if deleted:
        print(f"[migrations] 0002: deleted {deleted} orphaned director_state rows")

    # 4. Delete orphaned conversation_logs rows (conversation_id not in conversations)
    conn.execute(
        """
        DELETE FROM conversation_logs
        WHERE conversation_id NOT IN (SELECT id FROM conversations)
    """
    )
    deleted = conn.total_changes
    if deleted:
        print(f"[migrations] 0002: deleted {deleted} orphaned conversation logs")

    # 5. Ensure foreign keys are ON for future connections (already done by the
    #    PRAGMA above; this migration is idempotent).
