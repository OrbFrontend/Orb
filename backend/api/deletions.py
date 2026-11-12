"""The deletes the routes and the assistant's Apply share, each with the fences and follow-ups its route keeps.

A chat or document delete closes admission, settles the work running there, then deletes under the resource's lane. A message
delete takes its chat's lane and settles the media jobs rendering any message it removes. Each answers whether the row existed.
"""

from __future__ import annotations

from ..database import delete_conversation, delete_document, delete_message_with_descendants, mark_orphaned_changesets_stale
from ..database.queries.messages import get_message_subtree_ids
from .deps import conversation_stream_lock, deleting_message_sources, deleting_resources


async def delete_chat(cid: str) -> bool:
    """Delete chat *cid* once its running work has settled."""
    async with deleting_resources([cid]):
        if not await delete_conversation(cid):
            return False
    # The cascade NULLed the source pointers of every changeset raised in this chat. Unreviewed proposals lose the evidence they
    # were derived from and go stale; applied ones stay canon, carrying their denormalised labels.
    await mark_orphaned_changesets_stale()
    return True


async def delete_message_subtree(cid: str, message_id: int) -> bool:
    """Delete message *message_id* of chat *cid* and every message after it, on every branch."""
    # Serialize against an in-flight streaming pipeline on this cid: ON DELETE CASCADE on messages.parent_id would otherwise
    # wipe the in-flight assistant row mid-INSERT (IntegrityError) or right after commit (silent disappearance).
    async with conversation_stream_lock(cid), deleting_message_sources(cid, await get_message_subtree_ids(cid, message_id)):
        if not await delete_message_with_descendants(cid, message_id):
            return False
        # The cascade has already NULLed every changeset pointer into the deleted subtree, so orphanhood is what identifies the
        # affected proposals -- an id list read before the delete would match nothing after it. Applied history survives the
        # same cascade with its denormalised labels intact.
        await mark_orphaned_changesets_stale()
        return True


async def delete_document_settled(did: str) -> bool:
    """Delete document *did* once any generation into it has settled."""
    async with deleting_resources([f"doc:{did}"]):
        return await delete_document(did)
