"""Copies of a conversation: the member map a copied group needs, and a checkpoint of the active path.

Shared by the conversation routes and the assistant's ``checkpoint_chat``, so both copy exactly the same things.
"""

from __future__ import annotations

from ..database import (
    add_conversation_log,
    add_message,
    copy_state_events,
    decision_evaluations_of,
    fork_conversation,
    get_conversation,
    get_conversation_logs,
    get_director_state,
    get_group_members,
    get_messages,
    set_active_leaf,
    update_director_state,
    user_attachment_payloads,
)
from ..database.models import ConversationRow
from ..pipeline import remap_decision_anchors


async def copy_member_map(conv: ConversationRow, source_cid: str, new_cid: str) -> dict[str, str]:
    """Old member id -> new member id, for a copy of a group's messages.

    ``fork_conversation`` recreates the roster with fresh member ids, so every copied assistant row has to be re-pointed or the
    copy loses its speakers. ``speaker_key`` is the join: it is immutable and the fork carries it over, which ``id`` and
    ``display_name`` do not. Empty for a solo conversation.
    """
    if conv.get("kind", "solo") != "group":
        return {}
    old_members = await get_group_members(source_cid, include_inactive=True)
    new_by_key = {m["speaker_key"]: m["id"] for m in await get_group_members(new_cid, include_inactive=True)}
    return {m["id"]: new_by_key[m["speaker_key"]] for m in old_members if m["speaker_key"] in new_by_key}


def checkpoint_title(conv: ConversationRow, title: str | None) -> str:
    """The title a checkpoint of *conv* gets: *title* when one is given, else the chat's own with ``(checkpoint)``."""
    if title and title.strip():
        return title.strip()
    base = conv.get("title") or conv.get("character_name") or "Conversation"
    return f"{base} (checkpoint)"


async def checkpoint_conversation(source_cid: str, new_title: str) -> ConversationRow | None:
    """Copy a conversation's active path into a new conversation."""
    conv = await get_conversation(source_cid)
    if not conv:
        return None

    # Active path, root->leaf, with user_attachments already populated.
    messages = await get_messages(source_cid)

    new_cid = await fork_conversation(conv, new_title)
    member_map = await copy_member_map(conv, source_cid, new_cid)

    # Re-insert the path linearly, remapping parent_id and recording old->new
    # message ids so the conversation_logs below can be re-pointed onto the copy.
    id_map: dict[int, int] = {}
    prev_id: int | None = None
    for msg in messages:
        new_id, _ = await add_message(
            new_cid,
            msg["role"],
            msg["content"],
            msg["turn_index"],
            parent_id=prev_id,
            attachments=user_attachment_payloads(msg),
            fragment_cooldowns=msg.get("fragment_cooldowns") or {},
            speaker_member_id=member_map.get(str(msg.get("speaker_member_id"))) if msg.get("speaker_member_id") else None,
            exchange_id=msg.get("exchange_id"),
            persona_id=msg.get("persona_id"),
            persona_name=msg.get("persona_name"),
            writer_draft=msg.get("writer_draft"),
            # Anchors are remapped through the same id map the logs below use. The path is copied root-to-leaf, so a reply's own
            # anchor -- always an earlier row -- is already in the map by the time it is read.
            decision_evaluations=remap_decision_anchors(decision_evaluations_of(msg), id_map),
            decision_cooldowns=msg.get("decision_cooldowns") or {},
        )
        id_map[msg["id"]] = new_id
        prev_id = new_id

    if prev_id is not None:
        await set_active_leaf(new_cid, prev_id)

    # The path's state history, re-anchored through the same id map as the
    # decision anchors, so the copy keeps each value's history and source.
    await copy_state_events(source_cid, new_cid, id_map)

    # Carry the director state verbatim so the first turn on the checkpoint starts from the same moods as the original.
    director = await get_director_state(source_cid)
    await update_director_state(
        new_cid,
        director.get("active_moods", []),
        keywords=director.get("keywords", []),
        macro_choices=director.get("macro_choices", {}),
    )

    # Carry the per-turn inspector logs, re-pointing message_id onto the copied rows. Logs tied to messages off the active path
    # (other branches) or with no message_id resolve to None in id_map and are skipped.
    for log in await get_conversation_logs(source_cid):
        src_msg_id = log.get("message_id")
        new_msg_id = id_map.get(src_msg_id) if src_msg_id is not None else None
        if new_msg_id is None:
            continue
        await add_conversation_log(
            new_cid,
            log["turn_index"],
            log.get("tool_calls") or [],
            log.get("active_moods_after") or [],
            log.get("injection_block") or "",
            log.get("agent_latency_ms") or 0,
            message_id=new_msg_id,
            reasoning_director=log.get("reasoning_director") or "",
            reasoning_writer=log.get("reasoning_writer") or "",
            reasoning_editor=log.get("reasoning_editor") or "",
            feedback=log.get("feedback") or {},
            injection_without_moods=log.get("injection_block_without_moods"),
            latency_breakdown=log.get("latency_breakdown") or [],
        )

    return await get_conversation(new_cid)
