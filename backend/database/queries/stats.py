from __future__ import annotations

from ..connection import get_db


async def get_global_stats() -> dict:
    """Aggregate usage stats across the whole database.

    Returns raw counts/sums; callers derive words/tokens from total chars.
    """
    async with get_db() as db:
        conv_row = list(await db.execute_fetchall("SELECT COUNT(*) FROM conversations"))
        total_conversations = conv_row[0][0] if conv_row else 0

        # One full scan of messages: total chars cover ALL branches (swipes/regens
        # are sibling rows here) for the token estimate, while user_chars filters to
        # role='user' so "words written" reflects only what the user typed.
        msg_row = list(
            await db.execute_fetchall(
                """SELECT COUNT(*),
                          COALESCE(SUM(LENGTH(content)), 0),
                          COALESCE(SUM(CASE WHEN role = 'user' THEN LENGTH(content) ELSE 0 END), 0)
                   FROM messages"""
            )
        )
        total_messages = msg_row[0][0] if msg_row else 0
        total_chars = msg_row[0][1] if msg_row else 0
        user_chars = msg_row[0][2] if msg_row else 0

        # Favorite character = the one whose conversations hold the most messages.
        # Group on character_name (not card id) so renamed/deleted cards still tally,
        # skipping unnamed conversations.
        fav_row = list(
            await db.execute_fetchall(
                """SELECT c.character_name,
                          COUNT(*) AS msg_count,
                          COUNT(DISTINCT c.id) AS conv_count,
                          MAX(c.character_card_id) AS card_id
                   FROM messages m
                   JOIN conversations c ON c.id = m.conversation_id
                   WHERE c.character_name != ''
                   GROUP BY c.character_name
                   ORDER BY msg_count DESC
                   LIMIT 1"""
            )
        )
        favorite_character = (
            {
                "name": fav_row[0][0],
                "messages": fav_row[0][1],
                "conversations": fav_row[0][2],
                "card_id": fav_row[0][3],
            }
            if fav_row
            else None
        )

        # > 0 (not just IS NOT NULL): turns with no LLM passes log 0, and
        # averaging those in would understate true response time.
        lat_row = list(
            await db.execute_fetchall("SELECT AVG(agent_latency_ms) FROM conversation_logs WHERE agent_latency_ms > 0")
        )
        avg_latency = lat_row[0][0] if lat_row else None

        return {
            "total_conversations": total_conversations,
            "total_messages": total_messages,
            "total_chars": total_chars,
            "user_chars": user_chars,
            "favorite_character": favorite_character,
            "avg_latency_ms": avg_latency,
        }
