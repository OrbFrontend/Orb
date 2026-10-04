"""Turn machinery behind the on-demand prose rewrite of one saved assistant message."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .. import database as db
from ..database.models import CharacterCardRow, ConversationRow
from ..inference import AbortToken, KVCacheTracker, agent_lane_from_settings, client_from_settings
from ..workflows.prose_rewriter_host import RERUN_AFTER_REWRITE
from .workflow_bridge import PostPipelineResult, run_post_pipeline


def retained_draft(message: Mapping[str, Any]) -> str | None:
    """The row's retained pre-rewriter draft, when it carries real text.

    Blank counts as absent: a draft of ``""`` is not a source, and letting it
    through would have the client promise a rewrite of text that is not there.
    """
    draft = message.get("writer_draft")
    return draft if isinstance(draft, str) and draft.strip() else None


def prose_rewrite_source(message: Mapping[str, Any]) -> str | None:
    """Return the text an on-demand rewrite should use."""
    draft = retained_draft(message)
    if draft is not None:
        return draft
    content = message.get("content") or ""
    return content if content.strip() else None


async def _speaker_card(conv: ConversationRow | None, message: Mapping[str, Any]) -> tuple[str | None, CharacterCardRow | None]:
    """The card of the character who wrote *message*: the solo card, or the group member's."""
    character_id = conv.get("character_card_id") if conv else None
    if conv and conv.get("kind", "solo") == "group":
        member_id = message.get("speaker_member_id")
        member = await db.get_group_member(str(member_id), conversation_id=conv["id"]) if member_id else None
        character_id = member.get("character_card_id") if member else None
    return character_id, await db.get_character_card(character_id) if character_id else None


async def rerun_after_prose_rewrite(
    cid: str, message: Mapping[str, Any], draft: str, settings: Mapping[str, Any], abort_token: AbortToken
) -> str:
    """Re-run the post workflows a rewrite invalidates on *draft*, as the character who wrote *message*."""
    history = await db.get_messages_before(cid, message["id"])
    character_id, card = await _speaker_card(await db.get_conversation(cid), message)
    client = client_from_settings(settings, abort_token=abort_token)
    agent_client, agent_model_name = agent_lane_from_settings(settings, writer_client=client, abort_token=abort_token)
    post: PostPipelineResult | None = None
    async for event in run_post_pipeline(
        draft=draft,
        conversation_id=cid,
        character_id=character_id,
        card=card,
        history=history,
        effective_msg=next((m["content"] for m in reversed(history) if m["role"] == "user"), ""),
        director_output={},
        settings=settings,
        prefix=[],
        enabled_tools={},
        turn_scratch={},
        client=client,
        kv_tracker=KVCacheTracker(conversation_id=cid),
        schema_overrides={},
        agent_client=agent_client,
        agent_model_name=agent_model_name,
        post_workflow_ids=RERUN_AFTER_REWRITE,
    ):
        if isinstance(event, PostPipelineResult):
            post = event
    assert post is not None
    return post.draft
