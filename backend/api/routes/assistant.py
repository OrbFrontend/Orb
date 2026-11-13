"""The right-panel assistant: one run stream at a time, its Stop, and Apply for the edits it proposes."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import cast

from fastapi import APIRouter, HTTPException, Query, Request, Response

from ...core import scrub_log
from ...database import (
    get_character_card,
    get_conversation,
    get_document,
    get_lorebook_entry,
    get_settings,
    get_world,
    resolve_cast,
)
from ...features.assistant import AssistantUnavailable, RequestMark, ViewContext, run_assistant
from ...inference import AbortToken, agent_lane_from_settings, client_from_settings
from ...pipeline.events import FailureEvent
from ...workflows.contracts import PublicEvent
from ..assistant import (
    MediaRun,
    apply_proposals,
    build_tools,
    held_transcript,
    hold_transcript,
    open_proposals,
    pending_note,
    revise_proposal,
    rewind_note,
    rewound,
    undo_proposals,
)
from ..assistant.images import get_image
from ..deps import CleanupStreamingResponse, sse_stream, stop_active_stream
from ..schemas import AssistantApplyRequest, AssistantReviseRequest, AssistantRunRequest, AssistantViewContext

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
    """Check the captured ids and how they relate; the browser's word for either is not taken.

    The lorebook drawer's world and entry may accompany any view: the drawer opens over it.
    """
    view = await _resolve_page(context)
    if context.world_id is None:
        return view
    world = await get_world(context.world_id)
    if world is None:
        raise HTTPException(status_code=404, detail="World not found")
    view = replace(view, world_id=world["id"], world_name=world["name"])
    if context.entry_id is None:
        return view
    entry = await get_lorebook_entry(context.entry_id)
    if entry is None or entry["world_id"] != world["id"]:
        raise HTTPException(status_code=404, detail="Lore entry not found in that world")
    return replace(view, entry_id=entry["id"], entry_name=entry["name"])


async def _resolve_page(context: AssistantViewContext) -> ViewContext:
    """The page behind the drawer: a chat, a document, or the library with an optional character."""
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
    abort_token = AbortToken()
    # A media tool's render runs as a workflow job filed under this run, which the run's Stop cancels and waits for alone.
    progress: asyncio.Queue[str] = asyncio.Queue()
    media = MediaRun(job=f"assistant:{operation_id}", abort=abort_token, report=progress.put_nowait)
    # Read at Send: what the user's card still holds pending, by the ids it names; the texts are the server's own.
    held = open_proposals(data.pending)

    async def _gen() -> AsyncIterator[PublicEvent | FailureEvent]:
        try:
            # Read once the run is admitted. The browser is told whether the conversation it shows is the one the model reads:
            # after a restart, or a request from another browser, it is not.
            transcript = held_transcript(data.session)
            removed: tuple[RequestMark, ...] = ()
            if data.edit and transcript is not None:
                # The request replaces one already sent, and everything after it. Held at once, so the conversation is
                # the one the browser now shows even if this run never reaches the model.
                transcript, removed = rewound(transcript, data.request_id or "")
                hold_transcript(data.session, transcript)
            yield {"event": "start", "data": {"continued": bool(transcript and transcript.messages)}}
            client = client_from_settings(settings, abort_token=abort_token)
            agent_client, model = agent_lane_from_settings(settings, writer_client=client, abort_token=abort_token)
            async for event in run_assistant(
                agent_client,
                model,
                settings=settings,
                tools=build_tools(settings, view, media=media, held=held),
                transcript=transcript,
                request=data.text.strip(),
                view=view,
                note="\n\n".join(filter(None, (rewind_note(removed), pending_note(held)))),
                request_id=data.request_id or "",
                progress=progress,
                keep=lambda kept: hold_transcript(data.session, kept),
                reasoning=data.reasoning,
            ):
                payload = {key: value for key, value in event.items() if key != "type"}
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


@router.post("/api/assistant/undo")
async def api_undo_assistant_edits(data: AssistantApplyRequest):
    """Put back the stored text of each applied edit *proposal_ids* names, where it still holds what Apply wrote.

    ``undone`` makes the edit pending again; ``stale``, the text changed since; ``refused``, a delete or an edit not applied;
    ``missing``, ``busy`` and ``expired`` as for Apply.
    """
    return {"results": await undo_proposals(data.proposal_ids)}


@router.post("/api/assistant/revise")
async def api_revise_assistant_edit(data: AssistantReviseRequest):
    """Replace what a pending text edit writes with the user's own *text*; answers its new preview (``revised``), or
    ``refused`` or ``expired``."""
    return await revise_proposal(data.proposal_id, data.text)


@router.get("/api/assistant/images/{image_id}")
async def api_assistant_image(image_id: str):
    """A temporary assistant picture, shown inline; the browser saves it for manual avatar assignment and cropping."""
    image = get_image(image_id)
    if image is None:
        raise HTTPException(
            status_code=404, detail="This assistant picture expired or the server restarted. Generate it again."
        )
    return Response(
        image.data,
        media_type=image.mime,
        headers={
            "Content-Disposition": f'inline; filename="{image.filename}"',
            # An id names one picture for as long as it is held, so a browser that has it need not fetch it again.
            "Cache-Control": "private, max-age=86400, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
