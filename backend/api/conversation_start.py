"""Start a solo chat the way the conversation route does: from a card's fields or the caller's, with the greeting as the
first reply and the card's alternate greetings as its swipes."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Mapping
from typing import Any

from ..core import has_inline_macros, resolve_inline
from ..database import (
    add_message,
    create_conversation,
    insert_alternate_greeting_swipes,
    set_active_leaf,
    set_workflow_message_state,
)
from ..database.models import ConversationRow

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "New Conversation"


async def start_solo_chat(
    title: str,
    card: Mapping[str, Any] | None,
    *,
    char_name: str = "",
    char_scenario: str = "",
    first_mes: str = "",
    post_history_instructions: str = "",
) -> ConversationRow:
    """A new solo chat; *card*'s fields replace the given ones, and a default *title* takes the card's name."""
    cid = str(uuid.uuid4())
    card_id = None
    if card is not None:
        card_id = str(card["id"])
        char_name = card.get("name", "")
        char_scenario = card.get("scenario", "")
        first_mes = card.get("first_mes", "")
        post_history_instructions = card.get("post_history_instructions", "")
        if title == DEFAULT_TITLE:
            title = char_name
    conv = await create_conversation(
        cid=cid,
        title=title,
        char_name=char_name,
        char_scenario=char_scenario,
        post_history_instructions=post_history_instructions,
        character_card_id=card_id,
    )
    # Content is stored macro-resolved; the raw template rides the per-message "macros" slot so the greeting can re-roll freely
    # until the first user message freezes it (see reroll_unfrozen_greetings).
    if first_mes.strip():
        raw_greeting = first_mes.strip()
        msg_id, _ = await add_message(cid, "assistant", resolve_inline(raw_greeting), 0, attachments=None)
        await set_active_leaf(cid, msg_id)
        if has_inline_macros(raw_greeting):
            await set_workflow_message_state(msg_id, "macros", {"template": raw_greeting})
        if card is not None:
            count = await insert_alternate_greeting_swipes(cid, card.get("alternate_greetings") or [])
            if count:
                logger.info(f"Created {count} alternate greeting swipes for conversation {cid}")
    return conv
