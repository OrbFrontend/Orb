"""The right-panel assistant: one run stream at a time, its Stop, and Apply for the edits it proposes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request

from ...core import scrub_log
from ...database import data_version, get_character_card, get_conversation, get_document, get_settings, resolve_cast
from ...features.assistant import AssistantUnavailable, Continuation, HistoryItem, ViewContext, run_assistant
from ...inference import AbortToken, agent_lane_from_settings, client_from_settings
from ...pipeline.events import FailureEvent
from ...workflows.contracts import PublicEvent
from ..assistant import MediaRun, apply_proposals, build_tools, keep_follow_up, take_follow_up
from ..deps import CleanupStreamingResponse, sse_stream, stop_active_stream
from ..schemas import AssistantApplyRequest, AssistantRunRequest, AssistantViewContext

logger = logging.getLogger(__name__)

router = APIRouter()

# Chat streams are keyed by a conversation id, which is a UUID; the colon keeps this key out of that space, as the
# ``doc:`` and ``library:`` keys are.
ASSISTANT_STREAM_KEY = "assistant:run"

_OPERATION_ID = Query(min_length=1, max_length=64)


def _refuse(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


async def _character_name(character_id: str, *, required: bool = True) -> str | None:
    """The card's name; a missing card is a 404 when the browser named it, and ``None`` for a solo chat's own, since deleting a
    card keeps the chats that used it."""
    card = await get_character_card(character_id)
    if card is None and required:
        raise HTTPException(status_code=404, detail="Character not found")
    return None if card is None else str(card.get("name") or "")


async def resolve_view(context: AssistantViewContext) -> ViewContext:
    """Check the captured ids and how they relate; the browser's word for either is not taken."""
    if context.mode == "document":
        if context.conversation_id or context.character_id or not context.document_id:
            raise _refuse("A document view names a document and nothing else")
        document = await get_document(context.document_id)
        if document is None:
            raise HTTPException(status_code=404, detail="Document not found")
        return ViewContext("document", document_id=document["id"], document_title=document["title"])
    if context.document_id:
        raise _refuse("Only a document view names a document")
    if context.mode == "library":
        if context.conversation_id:
            raise _refuse("A library view names no chat")
        if not context.character_id:
            return ViewContext("library")
        return ViewContext(
            "library", character_id=context.character_id, character_name=await _character_name(context.character_id) or ""
        )
    if not context.conversation_id:
        raise _refuse("A chat view names its chat")
    conv = await get_conversation(context.conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    group = conv.get("kind") == "group"
    character_id, character_name = context.character_id, None
    if group:
        # No character is inferred for a group: only one the request names, and only from its cast.
        if character_id and character_id not in {member.card_id for member in (await resolve_cast(conv)).members}:
            raise _refuse("That character is not in this group chat")
    elif character_id and character_id != conv.get("character_card_id"):
        raise _refuse("That character is not this chat's character")
    if character_id:
        character_name = await _character_name(character_id)
    elif not group and (own := conv.get("character_card_id")):
        character_name = await _character_name(own, required=False)
        character_id = own if character_name is not None else None
    return ViewContext(
        "chat",
        conversation_id=conv["id"],
        conversation_title=conv["title"],
        group=group,
        character_id=character_id,
        character_name=character_name or "",
    )


@router.post("/api/assistant/run")
async def api_run_assistant(data: AssistantRunRequest, request: Request, operation_id: str = _OPERATION_ID):
    """Stream one assistant request. The run is registered under *operation_id*, which its Stop must name."""
    settings = await get_settings()
    view = await resolve_view(data.context)
    *earlier, latest = data.items
    abort_token = AbortToken()
    history = [HistoryItem(item.kind, item.text, item.tool, item.view) for item in earlier]
    # A media tool's render runs as a workflow job filed under this run, which the run's Stop cancels and waits for alone.
    progress: asyncio.Queue[str] = asyncio.Queue()
    media = MediaRun(job=f"assistant:{operation_id}", abort=abort_token, report=progress.put_nowait)

    async def _gen() -> AsyncIterator[PublicEvent | FailureEvent]:
        try:
            yield {"event": "start", "data": {}}
            # Taken once the run is admitted, so a request another run refused leaves the follow-up to the tab it belongs to.
            started = await data_version()
            resume, unchanged = take_follow_up(data.resume, started)
            kept: list[Continuation] = []
            wrote = False
            client = client_from_settings(settings, abort_token=abort_token)
            agent_client, model = agent_lane_from_settings(settings, writer_client=client, abort_token=abort_token)
            async for event in run_assistant(
                agent_client,
                model,
                settings=settings,
                tools=build_tools(settings, view, media=media),
                history=history,
                request=latest.text.strip(),
                view=view,
                progress=progress,
                resume=resume,
                unchanged=unchanged,
                keep=kept.append,
                reasoning=data.reasoning,
            ):
                payload = {key: value for key, value in event.items() if key != "type"}
                # A commit of the run's own leaves its results current; any other one during the run leaves them in doubt.
                wrote = wrote or event["type"] == "mutation"
                if event["type"] == "done" and kept:
                    settled_at = await data_version()
                    payload["resume"] = keep_follow_up(kept[-1], settled_at if wrote or started == settled_at else None)
                yield cast(PublicEvent, {"event": event["type"], "data": payload})
        except AssistantUnavailable as exc:
            logger.warning("Assistant run %s failed: %s", scrub_log(operation_id), exc)
            yield {"event": "error", "data": str(exc)}

    return CleanupStreamingResponse(
        sse_stream(_gen(), request, abort_token=abort_token, cid=ASSISTANT_STREAM_KEY), media_type="text/event-stream"
    )


@router.post("/api/assistant/stop")
async def api_stop_assistant(operation_id: str = _OPERATION_ID):
    """Stop the run *operation_id* started and wait for it to settle; a Stop naming an older run ends nothing."""
    return {"ok": True, **await stop_active_stream(ASSISTANT_STREAM_KEY, operation_id=operation_id)}


@router.post("/api/assistant/apply")
async def api_apply_assistant_edits(data: AssistantApplyRequest):
    """Commit the proposals *proposal_ids* name, each once, and answer one verdict for each, in order.

    ``applied``; ``stale``, its text changed after the preview; ``missing``, its target is gone; ``busy``, its chat is
    generating or being deleted, the one verdict worth retrying; ``expired``, unknown to the server or forgotten since. A
    repeated Apply returns the first one's answer.
    """
    return {"results": await apply_proposals(data.proposal_ids)}
