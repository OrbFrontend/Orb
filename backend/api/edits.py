"""Writes that more than one route makes, each with every invariant its original route kept."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..database import save_edited_message, sync_conversations_for_card, update_character_card
from ..database.models import CharacterCardRow


async def save_message_edit(original: Mapping[str, Any], content: str, *, expected: str | None = None) -> bool:
    """Replace a message's content as the edit route does; the caller holds its chat's stream lock.

    *content* is final: the caller resolves inline macros first, so what it previewed is what is saved. With *expected*, the
    stored content must still be that text (``StaleWrite`` otherwise), and ``False`` means the message is no longer in its chat.
    The edit and its invariants commit together, so no failure can leave one without the others.
    """
    assistant = original["role"] == "assistant"
    return await save_edited_message(
        original["id"],
        content,
        # Editing an unfrozen greeting drops its stashed template -- otherwise the next fetch would re-roll from it and clobber
        # the manual edit.
        drop_template=assistant and original["parent_id"] is None,
        # Same clobber, one surface over: the retained pre-rewriter draft describes the text this edit just replaced, and the
        # on-demand prose rewriter prefers it over the saved content -- so a rewrite after an edit would quietly restore the
        # pre-edit prose. Dropping it makes the rewriter fall back to what the user actually wrote, which is what they mean by
        # "rewrite this message".
        clear_draft=assistant,
        # Every edit also makes an unreviewed world-change proposal derived from this exact text go stale: editing either
        # source message invalidates that evidence, so the proposal must be re-evaluated rather than applied.
        expected=expected,
        conversation_id=original["conversation_id"],
    )


async def save_card_update(card_id: str, data: dict, *, old_name: str | None = None) -> CharacterCardRow | None:
    """Update a card and carry its prompt-facing fields to the chats that use it; ``None`` when there is no such card.

    *old_name* is the name before a rename, so chat titles still showing it follow. The assistant's reviewed edit makes the same
    two writes as separate steps, so a failure between them leaves a step its retry repeats.
    """
    result = await update_character_card(card_id, data)
    if not result:
        return None
    await sync_conversations_for_card(card_id, result, old_name=old_name)
    return result
