"""The raw Writer prompt behind each chat's latest reply."""

from __future__ import annotations

from ..connection import immediate_tx, select_rows
from ..models import ReplyPromptRow


async def get_reply_prompt(conversation_id: str) -> ReplyPromptRow | None:
    rows = await select_rows(
        "SELECT conversation_id, message_id, model, prompt, head FROM reply_prompts WHERE conversation_id = ?",
        (conversation_id,),
    )
    return ReplyPromptRow(**rows[0]) if rows else None


async def set_reply_prompt(conversation_id: str, message_id: int, model: str, prompt: str, head: str) -> None:
    """Store the chat's latest reply prompt, replacing the one before. Raises ``sqlite3.IntegrityError`` for a missing row."""
    async with immediate_tx() as db:
        await db.execute(
            "INSERT INTO reply_prompts (conversation_id, message_id, model, prompt, head) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET message_id = excluded.message_id, model = excluded.model, "
            "prompt = excluded.prompt, head = excluded.head",
            (conversation_id, message_id, model, prompt, head),
        )


async def drop_reply_prompt_outdated_by(db, edited_message_id: int) -> None:
    """Drop the chat's stored prompt once another message's text changed: it quotes the text as it was."""
    await db.execute(
        "DELETE FROM reply_prompts WHERE message_id != ? "
        "AND conversation_id = (SELECT conversation_id FROM messages WHERE id = ?)",
        (edited_message_id, edited_message_id),
    )
