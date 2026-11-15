"""The assistant's conversations, held by the server between requests and restarts so each one extends what the model read."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict, replace
from typing import Any

from ...database import delete_assistant_chat, get_active_path, get_assistant_chat, put_assistant_chat
from .contracts import ChatSeen, RequestMark, Transcript, is_request_message

logger = logging.getLogger(__name__)

# The conversations kept, each named by the id its browser made for it: one per character there, and other browsers' own.
MAX_HELD = 32

# The most recently held, as stored. Every write goes through one lock in the order it was held, so the stored row is the last.
_held: OrderedDict[str, Transcript] = OrderedDict()
_write_lock = asyncio.Lock()
_writes: set[asyncio.Task[None]] = set()


def _encoded(transcript: Transcript) -> dict[str, Any]:
    return asdict(transcript)


def _chat(raw: dict[str, Any] | None) -> ChatSeen | None:
    if not raw:
        return None
    return ChatSeen(raw["conversation_id"], raw["length"], tuple((length, digest) for length, digest in raw["prefixes"]))


def _decoded(raw: dict[str, Any]) -> Transcript:
    marks = tuple(RequestMark(mark["id"], tuple(mark["committed"]), _chat(mark.get("chat"))) for mark in raw["requests"])
    return Transcript(tuple(raw["messages"]), raw["model"], raw["endpoint"], marks, tuple(raw.get("awaiting", ())))


def _remember(session: str, transcript: Transcript) -> None:
    _held[session] = transcript
    _held.move_to_end(session)
    while len(_held) > MAX_HELD:
        _held.popitem(last=False)


async def held_transcript(session: str) -> Transcript | None:
    """The conversation *session* names, from before a restart too, or ``None`` when none is kept."""
    if (transcript := _held.get(session)) is not None:
        _held.move_to_end(session)
        return transcript
    # After the writes already waiting, so a row being stored is read as stored.
    try:
        async with _write_lock:
            raw = await get_assistant_chat(session)
        if raw is None:
            return None
        transcript = _decoded(raw)
    except (KeyError, TypeError, ValueError):
        logger.warning("Dropping an unreadable stored assistant conversation")
        return None
    _remember(session, transcript)
    return transcript


async def _store(session: str, transcript: Transcript) -> None:
    async with _write_lock:
        try:
            await put_assistant_chat(session, _encoded(transcript), MAX_HELD)
        except Exception:
            logger.exception("Storing the assistant conversation failed")


async def flush_transcripts() -> None:
    """Wait until every conversation held so far is stored. A Stop while waiting leaves the writes to finish."""
    if _writes:
        await asyncio.wait(set(_writes))


async def forget_transcript(session: str) -> None:
    """Drop *session*'s conversation from memory and disk, after any write of it still waiting."""
    _held.pop(session, None)
    await flush_transcripts()
    async with _write_lock:
        await delete_assistant_chat(session)


def hold_transcript(session: str, transcript: Transcript) -> None:
    """Keep *transcript* as *session*'s conversation, now in memory and on disk once the writes before it are done."""
    _remember(session, transcript)
    task = asyncio.get_running_loop().create_task(_store(session, transcript))
    _writes.add(task)
    task.add_done_callback(_writes.discard)


def rewound(transcript: Transcript, request_id: str) -> tuple[Transcript, tuple[RequestMark, ...]]:
    """*transcript* as it stood before the request *request_id*, and the marks of that request and every one after it.

    This is how an edited request is taken: the conversation goes back to where the request began and the new wording
    extends it from there. Nothing else goes back: what the removed requests changed stays changed. A request the
    conversation no longer holds leaves nothing to extend, so the edit starts a conversation of its own.
    """
    starts = [index for index, message in enumerate(transcript.messages) if is_request_message(message)]
    marks = transcript.requests
    from_end = next((len(marks) - at for at, mark in enumerate(marks) if mark.id == request_id), 0)
    if not 0 < from_end <= len(starts):
        return replace(transcript, messages=(), requests=(), awaiting=()), ()
    kept = len(marks) - from_end
    return replace(transcript, messages=transcript.messages[: starts[-from_end]], requests=marks[:kept]), marks[kept:]


def rewind_note(removed: tuple[RequestMark, ...]) -> str:
    """What an edited request is told of the requests it replaced: only the changes they committed, which the model can no
    longer see being made. Empty when they changed nothing."""
    committed = [line for mark in removed for line in mark.committed]
    if not committed:
        return ""
    return (
        "[The user rewrote this request. The requests that followed its earlier wording were removed from this "
        "conversation, but what they changed was not undone and still stands:\n"
        + "\n".join(f"- {line}" for line in committed)
        + "]"
    )


def _checkpoints(length: int) -> list[int]:
    back = [0, *(1 << power for power in range(length.bit_length()))]
    return [length - step for step in back if step < length]


def _digests(path: Sequence[Mapping[str, Any]], lengths: Collection[int]) -> dict[int, str]:
    hashed = hashlib.blake2b(digest_size=12)
    found: dict[int, str] = {}
    for count, message in enumerate(path, 1):
        hashed.update(f"{message['id']}\x00{message['content'] or ''}\x00".encode())
        if count in lengths:
            found[count] = hashed.hexdigest()
    return found


async def chat_moved(transcript: Transcript | None, conversation_id: str) -> tuple[ChatSeen, str]:
    """The chat in view as it stands at Send, to keep with the request, and what the request is told of how it moved on
    since the last request sent from it: new messages, or where its kept branch stopped being what the model may have
    read. The note is empty when nothing did, or no earlier request still held saw the chat."""
    path = await get_active_path(conversation_id)
    lengths = _checkpoints(len(path))
    digests = _digests(path, lengths)
    now = ChatSeen(conversation_id, len(path), tuple((length, digests[length]) for length in lengths))
    marks = transcript.requests if transcript else ()
    before = next((mark.chat for mark in reversed(marks) if mark.chat and mark.chat.conversation_id == conversation_id), None)
    if before is None or before == now:
        return now, ""
    digests = _digests(path, {length for length, _ in before.prefixes})
    same = next((length for length, digest in before.prefixes if digests.get(length) == digest), 0)
    since = "since the last request sent from this chat"
    if same == before.length:
        first = same + 1
        added = (
            f"1 new message, #{first}"
            if first == now.length
            else f"{now.length - same} new messages, #{first} to #{now.length}"
        )
        return now, (
            f"[The chat in view moved on {since}: {added} on its kept branch. Read them before answering about the chat.]"
        )
    if same == now.length:
        ends = f"now ends at #{same}" if same else "is now empty"
        return now, (
            f"[The chat in view was cut back {since}: its kept branch {ends}. Later messages you read are no longer on it.]"
        )
    kept = {0: "", 1: "#1 is as it was. "}.get(same, f"Messages #1 to #{same} are as they were. ")
    return now, (
        f"[The chat in view changed {since}: a message was edited, deleted or regenerated, or another branch was kept. "
        f"{kept}From #{same + 1} on, what you read may be out of date; its kept branch now ends at #{now.length}. Read "
        "those again before answering about the chat.]"
    )
