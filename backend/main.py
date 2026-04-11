from __future__ import annotations
import json
import uuid
import re
import logging
import base64
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from typing import Optional
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import os

from .database import (
    init_db, get_settings, update_settings,
    get_fragments, get_fragment, create_fragment, update_fragment, delete_fragment,
    list_conversations, get_conversation, create_conversation, delete_conversation,
    get_messages, get_messages_with_branch_info,
    get_director_state, get_conversation_logs,
    list_character_cards, get_character_card, create_character_card,
    update_character_card, delete_character_card, get_character_avatar,
    add_message, set_active_leaf, get_message_by_id, switch_to_branch,
    delete_message_with_descendants, get_db,
)
import asyncio
from .orchestrator import handle_turn, handle_regenerate
from . import tavern_cards

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="Orb", lifespan=lifespan)


# ── Pydantic models ──

class SettingsUpdate(BaseModel):
    model_config = {"protected_namespaces": ()}

    endpoint_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    temperature: Optional[float] = None
    min_p: Optional[float] = None
    top_k: Optional[int] = None
    top_p: Optional[float] = None
    repetition_penalty: Optional[float] = None
    max_tokens: Optional[int] = None
    system_prompt: Optional[str] = None
    user_name: Optional[str] = None
    user_description: Optional[str] = None
    enabled_tools: Optional[dict] = None
    enable_agent: Optional[bool] = None
    length_guard_enabled: Optional[bool] = None
    length_guard_max_words: Optional[int] = None
    length_guard_max_paragraphs: Optional[int] = None


class FragmentCreate(BaseModel):
    id: str
    label: str
    description: str
    prompt_text: str
    negative_prompt: str = ""
    enabled: bool = True


class FragmentUpdate(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    prompt_text: Optional[str] = None
    negative_prompt: Optional[str] = None
    enabled: Optional[bool] = None


class ConversationCreate(BaseModel):
    title: str = "New Conversation"
    character_card_id: Optional[str] = None
    character_name: str = ""
    character_scenario: str = ""
    first_mes: str = ""
    post_history_instructions: str = ""


class CharacterCardCreate(BaseModel):
    name: str
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    mes_example: str = ""
    creator_notes: str = ""
    system_prompt: str = ""
    post_history_instructions: str = ""
    tags: list[str] = []
    creator: str = ""
    alternate_greetings: list[str] = []


class CharacterCardUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    personality: Optional[str] = None
    scenario: Optional[str] = None
    first_mes: Optional[str] = None
    mes_example: Optional[str] = None
    creator_notes: Optional[str] = None
    system_prompt: Optional[str] = None
    post_history_instructions: Optional[str] = None
    tags: Optional[list[str]] = None
    creator: Optional[str] = None
    alternate_greetings: Optional[list[str]] = None


class SendMessage(BaseModel):
    content: str
    enable_agent: bool = True
    turn_index: Optional[int] = None


class EditMessage(BaseModel):
    content: str
    regenerate: bool = True
    enable_agent: bool = True


class SwitchSwipe(BaseModel):
    swipe_index: int


class RegenerateMsg(BaseModel):
    enable_agent: bool = True


# ── Settings ──

@app.get("/api/settings")
async def api_get_settings():
    return await get_settings()


@app.put("/api/settings")
async def api_update_settings(data: SettingsUpdate):
    return await update_settings(data.model_dump(exclude_none=True))


# ── Fragments ──

@app.get("/api/fragments")
async def api_list_fragments():
    return await get_fragments()


@app.post("/api/fragments")
async def api_create_fragment(data: FragmentCreate):
    existing = await get_fragment(data.id)
    if existing:
        raise HTTPException(400, "Fragment with this ID already exists")
    return await create_fragment(data.model_dump())


@app.put("/api/fragments/{fid}")
async def api_update_fragment(fid: str, data: FragmentUpdate):
    result = await update_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(404, "Fragment not found")
    return result


@app.delete("/api/fragments/{fid}")
async def api_delete_fragment(fid: str):
    if not await delete_fragment(fid):
        raise HTTPException(404, "Fragment not found or is built-in")
    return {"ok": True}


# ── Conversations ──

@app.get("/api/conversations")
async def api_list_conversations():
    return await list_conversations()


@app.post("/api/conversations")
async def api_create_conversation(data: ConversationCreate):
    settings = await get_settings()
    cid = str(uuid.uuid4())

    char_name = data.character_name
    char_scenario = data.character_scenario
    first_mes = data.first_mes
    post_hist = data.post_history_instructions
    card_id = data.character_card_id
    title = data.title

    # If a character card is specified, pull fields from it
    if card_id:
        card = await get_character_card(card_id)
        if not card:
            raise HTTPException(404, "Character card not found")
        char_name = card["name"]
        char_scenario = card.get("scenario", "")
        first_mes = card.get("first_mes", "")
        post_hist = card.get("post_history_instructions", "")
        if title == "New Conversation":
            title = char_name

    conv = await create_conversation(
        cid=cid,
        title=title,
        char_name=char_name,
        char_scenario=char_scenario,
        first_mes=first_mes,
        post_history_instructions=post_hist,
        character_card_id=card_id,
    )

    # If there's a first message, auto-add it as the first assistant turn
    if first_mes.strip():
        msg_id = await add_message(cid, "assistant", first_mes.strip(), 0)
        await set_active_leaf(cid, msg_id)
        
        # If we have a character card with alternate greetings, create swipe versions for them
        if card_id:
            card = await get_character_card(card_id)
            if card and "alternate_greetings" in card:
                alternate_greetings = card.get("alternate_greetings", [])
                logger.info(f"Creating {len(alternate_greetings)} alternate greeting swipes for conversation {cid}")
                for i, greeting in enumerate(alternate_greetings):
                    if greeting and greeting.strip():
                        # Create swipe with increasing swipe_index (starting from 1 since 0 is the first_mes)
                        swipe_index = i + 1
                        # Create inactive swipe (is_active=0)
                        db = await get_db()
                        try:
                            now = datetime.now(timezone.utc).isoformat()
                            await db.execute(
                                "INSERT INTO messages (conversation_id, role, content, turn_index, swipe_index, is_active, parent_id, created_at) VALUES (?, ?, ?, ?, ?, 0, NULL, ?)",
                                (cid, "assistant", greeting.strip(), 0, swipe_index, now),
                            )
                            await db.commit()
                            logger.info(f"Created alternate greeting swipe {swipe_index}: {greeting[:50]}...")
                        finally:
                            await db.close()

    return conv


@app.delete("/api/conversations/{cid}")
async def api_delete_conversation(cid: str):
    if not await delete_conversation(cid):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


# ── Character Cards ──

def _slugify(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return slug[:60] if slug else "character"


@app.get("/api/characters")
async def api_list_characters():
    return await list_character_cards()


@app.post("/api/characters")
async def api_create_character(data: CharacterCardCreate):
    slug = _slugify(data.name)
    # Ensure unique ID
    base_slug = slug
    counter = 1
    while await get_character_card(slug):
        slug = f"{base_slug}-{counter}"
        counter += 1

    card_data = data.model_dump()
    card_data["id"] = slug
    card_data["source_format"] = "manual"
    return await create_character_card(card_data)


@app.post("/api/characters/import")
async def api_import_character(file: UploadFile = File(...)):
    """Import a SillyTavern-compatible character card PNG."""
    if not file.filename or not file.filename.lower().endswith(".png"):
        raise HTTPException(400, "Only .png character card files are supported")

    # Save to temp file for the parser
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        card = tavern_cards.parse(tmp_path)
        card_dict = tavern_cards.card_to_dict(card)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("Failed to parse tavern card")
        raise HTTPException(400, f"Failed to parse character card: {e}")
    finally:
        os.unlink(tmp_path)

    # Extract avatar from the PNG image itself
    avatar_b64 = base64.b64encode(content).decode("ascii")
    avatar_mime = "image/png"

    # Generate slug
    slug = _slugify(card_dict["name"]) if card_dict["name"] else "imported-character"
    base_slug = slug
    counter = 1
    while await get_character_card(slug):
        slug = f"{base_slug}-{counter}"
        counter += 1

    card_dict["id"] = slug
    card_dict["avatar_b64"] = avatar_b64
    card_dict["avatar_mime"] = avatar_mime

    result = await create_character_card(card_dict)
    return result


@app.get("/api/characters/{card_id}")
async def api_get_character(card_id: str):
    card = await get_character_card(card_id)
    if not card:
        raise HTTPException(404, "Character card not found")
    return card


@app.put("/api/characters/{card_id}")
async def api_update_character(card_id: str, data: CharacterCardUpdate):
    result = await update_character_card(card_id, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(404, "Character card not found")
    return result


@app.delete("/api/characters/{card_id}")
async def api_delete_character(card_id: str):
    if not await delete_character_card(card_id):
        raise HTTPException(404, "Character card not found")
    return {"ok": True}


@app.get("/api/characters/{card_id}/avatar")
async def api_get_avatar(card_id: str):
    result = await get_character_avatar(card_id)
    if not result:
        raise HTTPException(404, "No avatar found")
    image_bytes, mime_type = result
    return Response(content=image_bytes, media_type=mime_type or "image/png")


class _CleanupStreamingResponse(StreamingResponse):
    """StreamingResponse that guarantees the body async generator is closed
    even when the client disconnects mid-stream.

    Starlette's default StreamingResponse does NOT close the body iterator
    when send() fails due to client disconnect. This subclass ensures proper
    cleanup so that orchestrator finally blocks (which save incomplete messages
    on abort) always execute.
    """

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Always close the body generator, even if send() raised
            # (e.g. client disconnected). This ensures the orchestrator's
            # finally block runs and saves any incomplete message.
            if hasattr(self.body_iterator, "aclose"):
                try:
                    await asyncio.shield(self.body_iterator.aclose())
                except asyncio.CancelledError:
                    # Shield was cancelled; try once more
                    try:
                        await self.body_iterator.aclose()
                    except Exception:
                        pass


async def _sse_stream(gen, request: Request):
    """Wrap an event-dict async generator as SSE, stopping cleanly on client disconnect.

    When the client disconnects, aclose() propagates GeneratorExit through the
    entire async-generator chain (orchestrator → llm_client → httpx), which
    closes the upstream LLM connection rather than leaving it running.

    The aclose() is shielded from cancellation so that the orchestrator's
    finally block (which saves incomplete messages on abort) can always complete
    its database writes even if the asyncio task is being cancelled.
    """
    try:
        async for event in gen:
            if await request.is_disconnected():
                break
            evt_type = event["event"]
            evt_data = event.get("data", "")
            if isinstance(evt_data, dict):
                evt_data = json.dumps(evt_data)
            elif isinstance(evt_data, str):
                evt_data = evt_data.replace('\n', '\\n')
            yield f"event: {evt_type}\ndata: {evt_data}\n\n"
    finally:
        # Shield aclose() from CancelledError so the orchestrator's finally
        # block (fallback persistence of incomplete messages) always runs.
        try:
            await asyncio.shield(gen.aclose())
        except asyncio.CancelledError:
            # If shield itself is cancelled (extremely rare), still wait
            # for the close to finish synchronously
            try:
                await gen.aclose()
            except Exception:
                pass


@app.get("/api/conversations/{cid}/messages")
async def api_get_messages(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/edit")
async def api_edit_message(cid: str, msg_id: int, data: EditMessage, request: Request):
    """Edit a message by creating a sibling branch. Old branches are preserved.
    If editing a user message and regenerate=True, streams a new assistant response."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    original = await get_message_by_id(msg_id)
    if not original or original["conversation_id"] != cid:
        raise HTTPException(404, "Message not found")

    # Create sibling (same parent_id as original)
    new_msg_id = await add_message(
        cid, original["role"], data.content,
        original["turn_index"], parent_id=original.get("parent_id"),
    )
    await set_active_leaf(cid, new_msg_id)

    should_stream_regen = (original["role"] == "user" and data.regenerate)

    if should_stream_regen:
        return _CleanupStreamingResponse(
            _sse_stream(handle_turn(cid, data.content, skip_user_persist=True), request),
            media_type="text/event-stream",
        )

    return {"ok": True}


@app.delete("/api/conversations/{cid}/messages/{msg_id}")
async def api_delete_message(cid: str, msg_id: int):
    """Delete a message and all its descendants. Returns updated message list."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    if not await delete_message_with_descendants(cid, msg_id):
        raise HTTPException(404, "Message not found")
    return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/switch-branch")
async def api_switch_branch(cid: str, msg_id: int):
    """Switch to the branch containing msg_id (sets active leaf to deepest descendant)."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    success = await switch_to_branch(cid, msg_id)
    if not success:
        raise HTTPException(404, "Message not found")
    return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/regenerate")
async def api_regenerate_msg(cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None):
    """Regenerate a specific assistant message as a new sibling branch."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    return _CleanupStreamingResponse(
        _sse_stream(handle_regenerate(cid, msg_id), request),
        media_type="text/event-stream",
    )


@app.get("/api/conversations/{cid}/director")
async def api_get_director_state(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return await get_director_state(cid)


@app.get("/api/conversations/{cid}/logs")
async def api_get_logs(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    return await get_conversation_logs(cid)


# ── Chat (SSE streaming) ──

@app.post("/api/conversations/{cid}/send")
async def api_send_message(cid: str, data: SendMessage, request: Request):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    return _CleanupStreamingResponse(
        _sse_stream(handle_turn(cid, data.content), request),
        media_type="text/event-stream",
    )


# ── Frontend serving ──

@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# Mount static files last
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
