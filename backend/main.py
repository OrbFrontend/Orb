from __future__ import annotations
import hashlib
import json
import uuid
import logging
import base64
import tempfile
from contextlib import asynccontextmanager

from typing import Annotated, Optional, List
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import os

from .database import (
    init_db,
    get_settings,
    update_settings,
    get_endpoints,
    get_endpoint,
    create_endpoint,
    update_endpoint,
    delete_endpoint,
    get_model_configs,
    create_model_config,
    update_model_config,
    delete_model_config,
    get_mood_fragments,
    get_mood_fragment,
    create_mood_fragment,
    update_mood_fragment,
    delete_mood_fragment,
    list_conversations,
    get_conversation,
    create_conversation,
    delete_conversation,
    touch_conversation,
    get_messages_with_branch_info,
    get_director_state,
    get_conversation_logs,
    list_character_cards,
    get_character_card,
    create_character_card,
    update_character_card,
    delete_character_card,
    get_character_avatar,
    sync_conversations_for_card,
    insert_alternate_greeting_swipes,
    add_message,
    get_attachments_for_message,
    set_active_leaf,
    get_message_by_id,
    switch_to_branch,
    delete_message_with_descendants,
    update_message_content,
    get_phrase_bank_rows,
    add_phrase_group,
    update_phrase_group,
    delete_phrase_group,
    get_user_personas,
    create_user_persona,
    update_user_persona,
    delete_user_persona,
    get_director_fragments,
    get_director_fragment,
    create_director_fragment,
    update_director_fragment,
    delete_director_fragment,
    reset_to_defaults,
    get_messages,
)
import asyncio
from .orchestrator import handle_turn, handle_regenerate
from . import tavern_cards

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="Orb", lifespan=lifespan)

# Active LLM generations keyed by conversation ID.
# Populated when streaming starts; cleared when it ends or is aborted.
_active_clients: dict[str, object] = {}


@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


# Pydantic models ──


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
    reasoning_enabled_passes: Optional[dict] = None
    active_persona_id: Optional[int] = None
    character_library_view: Optional[str] = None
    character_library_sort: Optional[str] = None
    active_endpoint_id: Optional[int] = None
    active_model_config_id: Optional[int] = None


class EndpointCreate(BaseModel):
    url: str
    api_key: str = ""


class EndpointUpdate(BaseModel):
    url: Optional[str] = None
    api_key: Optional[str] = None


class ModelConfigCreate(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_name: str
    system_prompt: str = ""
    temperature: float = 0.8
    min_p: float = 0.0
    top_k: int = 40
    top_p: float = 0.95
    repetition_penalty: float = 1.0
    max_tokens: int = 4096


class ModelConfigUpdate(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_name: Optional[str] = None
    system_prompt: Optional[str] = None
    temperature: Optional[float] = None
    min_p: Optional[float] = None
    top_k: Optional[int] = None
    top_p: Optional[float] = None
    repetition_penalty: Optional[float] = None
    max_tokens: Optional[int] = None


class MoodFragmentCreate(BaseModel):
    id: str
    label: str
    description: str
    prompt_text: str
    negative_prompt: str = ""
    enabled: bool = True


class MoodFragmentUpdate(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    prompt_text: Optional[str] = None
    negative_prompt: Optional[str] = None
    enabled: Optional[bool] = None


class DirectorFragmentCreate(BaseModel):
    id: str
    label: str
    description: str
    field_type: str = "string"
    required: bool = False
    enabled: bool = True
    injection_label: str
    sort_order: int = 0


class DirectorFragmentUpdate(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    field_type: Optional[str] = None
    required: Optional[bool] = None
    enabled: Optional[bool] = None
    injection_label: Optional[str] = None
    sort_order: Optional[int] = None


class ConversationCreate(BaseModel):
    title: str = "New Conversation"
    character_card_id: Optional[str] = None
    character_name: str = ""
    character_scenario: str = ""
    first_mes: str = ""
    post_history_instructions: str = ""


class CharacterCardCreate(BaseModel):
    # id and source_format are normally omitted (manual creation). They are
    # supplied by the import flow: /api/characters/import parses the PNG and
    # computes a stable deterministic ID (orb_id embedded in the card, or a
    # SHA-256-derived UUID of the raw bytes), then the frontend passes it back
    # here on Save. Preserving the original ID means re-importing a card after
    # deletion relinks its conversation history instead of creating an orphan.
    id: Optional[str] = None
    source_format: Optional[str] = None
    name: str
    description: str = ""

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("name must not be empty or whitespace-only")
        return stripped

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
    avatar_b64: Optional[str] = None
    avatar_mime: Optional[str] = None


class CharacterCardUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            stripped = v.strip()
            if not stripped:
                raise ValueError("name must not be empty or whitespace-only")
            return stripped
        return v

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
    avatar_b64: Optional[str] = None
    avatar_mime: Optional[str] = None


class AttachmentIn(BaseModel):
    b64: str
    mime: str
    filename: Optional[str] = None
    size: Optional[int] = None

    @field_validator("size")
    @classmethod
    def validate_size(cls, v):
        if v is not None and v > 10 * 1024 * 1024:  # 10 MB
            raise ValueError("Attachment size exceeds 10 MB limit")
        return v

    @field_validator("b64")
    @classmethod
    def validate_b64(cls, v):
        # Ensure it's valid base64 (optional)
        import base64

        try:
            base64.b64decode(v, validate=True)
        except Exception:
            raise ValueError("Invalid base64 string")
        return v


class SendMessage(BaseModel):
    content: str
    enable_agent: bool = True
    turn_index: Optional[int] = None
    attachments: List[AttachmentIn] = []


class EditMessage(BaseModel):
    content: str
    regenerate: bool = True
    enable_agent: bool = True
    attachments: List[AttachmentIn] = []


class SwitchSwipe(BaseModel):
    swipe_index: int


class RegenerateMsg(BaseModel):
    enable_agent: bool = True


class PhraseGroupCreate(BaseModel):
    variants: list[str]


class PhraseGroupUpdate(BaseModel):
    variants: list[str]


class UserPersonaCreate(BaseModel):
    name: str
    description: str = ""
    avatar_color: Optional[str] = None


class UserPersonaUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    avatar_color: Optional[str] = None


# Settings ──


@app.get("/api/settings")
async def api_get_settings():
    return await get_settings()


@app.put("/api/settings")
async def api_update_settings(data: SettingsUpdate):
    return await update_settings(data.model_dump(exclude_unset=True))


# Endpoints ──


@app.get("/api/endpoints")
async def api_get_endpoints():
    return await get_endpoints()


@app.get("/api/endpoints/{endpoint_id}")
async def api_get_endpoint(endpoint_id: int):
    result = await get_endpoint(endpoint_id)
    if not result:
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return result


@app.post("/api/endpoints")
async def api_create_endpoint(data: EndpointCreate):
    return await create_endpoint(data.url, data.api_key)


@app.put("/api/endpoints/{endpoint_id}")
async def api_update_endpoint(endpoint_id: int, data: EndpointUpdate):
    result = await update_endpoint(endpoint_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(404, "Endpoint not found")
    return result


@app.delete("/api/endpoints/{endpoint_id}")
async def api_delete_endpoint(endpoint_id: int):
    if not await delete_endpoint(endpoint_id):
        raise HTTPException(404, "Endpoint not found")
    return {"ok": True}


@app.get("/api/endpoints/{endpoint_id}/models")
async def api_get_model_configs(endpoint_id: int):
    return await get_model_configs(endpoint_id)


@app.post("/api/endpoints/{endpoint_id}/models")
async def api_create_model_config(endpoint_id: int, data: ModelConfigCreate):
    try:
        return await create_model_config(endpoint_id, data.model_dump())
    except Exception as e:
        if "FOREIGN KEY constraint failed" in str(e):
            raise HTTPException(404, "Endpoint not found")
        raise


@app.put("/api/models/{config_id}")
async def api_update_model_config(config_id: int, data: ModelConfigUpdate):
    result = await update_model_config(config_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(404, "Model config not found")
    return result


@app.delete("/api/models/{config_id}")
async def api_delete_model_config(config_id: int):
    if not await delete_model_config(config_id):
        raise HTTPException(404, "Model config not found")
    return {"ok": True}


# Mood Fragments ──


@app.get("/api/fragments")
async def api_list_mood_fragments():
    return await get_mood_fragments()


@app.post("/api/fragments")
async def api_create_mood_fragment(data: MoodFragmentCreate):
    existing = await get_mood_fragment(data.id)
    if existing:
        raise HTTPException(400, "Mood fragment with this ID already exists")
    return await create_mood_fragment(data.model_dump())


@app.put("/api/fragments/{fid}")
async def api_update_mood_fragment(fid: str, data: MoodFragmentUpdate):
    result = await update_mood_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(404, "Mood fragment not found")
    return result


@app.delete("/api/fragments/{fid}")
async def api_delete_mood_fragment(fid: str):
    if not await delete_mood_fragment(fid):
        raise HTTPException(404, "Mood fragment not found or is built-in")
    return {"ok": True}


# Director Fragments ──


@app.get("/api/director-fragments")
async def api_list_director_fragments():
    return await get_director_fragments()


@app.post("/api/director-fragments")
async def api_create_director_fragment(data: DirectorFragmentCreate):
    existing = await get_director_fragment(data.id)
    if existing:
        raise HTTPException(400, "Director fragment with this ID already exists")
    result = await create_director_fragment(data.model_dump())
    if not result:
        raise HTTPException(500, "Failed to create director fragment")
    return result


@app.put("/api/director-fragments/{fid}")
async def api_update_director_fragment(fid: str, data: DirectorFragmentUpdate):
    result = await update_director_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(404, "Director fragment not found")
    return result


@app.delete("/api/director-fragments/{fid}")
async def api_delete_director_fragment(fid: str):
    if not await delete_director_fragment(fid):
        raise HTTPException(404, "Director fragment not found")
    return {"ok": True}


# Phrase Bank ──


@app.get("/api/phrase-bank")
async def api_get_phrase_bank():
    """Return phrase bank rows with ids for UI management."""
    return await get_phrase_bank_rows()


@app.post("/api/phrase-bank")
async def api_create_phrase_group(data: PhraseGroupCreate):
    """Create a new phrase variant group."""
    if not data.variants or len(data.variants) == 0:
        raise HTTPException(400, "At least one variant is required")
    # Validate all variants are strings
    for v in data.variants:
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(400, "All variants must be non-empty strings")
    group_id = await add_phrase_group(data.variants)
    return {"id": group_id, "variants": data.variants}


@app.put("/api/phrase-bank/{group_id}")
async def api_update_phrase_group(group_id: int, data: PhraseGroupUpdate):
    """Update an existing phrase variant group."""
    if not data.variants or len(data.variants) == 0:
        raise HTTPException(400, "At least one variant is required")
    # Validate all variants are strings
    for v in data.variants:
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(400, "All variants must be non-empty strings")
    success = await update_phrase_group(group_id, data.variants)
    if not success:
        raise HTTPException(404, "Phrase group not found")
    return {"ok": True, "id": group_id, "variants": data.variants}


@app.delete("/api/phrase-bank/{group_id}")
async def api_delete_phrase_group(group_id: int):
    """Delete a phrase variant group."""
    success = await delete_phrase_group(group_id)
    if not success:
        raise HTTPException(404, "Phrase group not found")
    return {"ok": True}


# User Personas ──


@app.get("/api/user-personas")
async def api_list_user_personas():
    return await get_user_personas()


@app.post("/api/user-personas")
async def api_create_user_persona(data: UserPersonaCreate):
    return await create_user_persona(data.model_dump())


@app.put("/api/user-personas/{persona_id}")
async def api_update_user_persona(persona_id: int, data: UserPersonaUpdate):
    result = await update_user_persona(persona_id, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(404, "User persona not found")
    return result


@app.delete("/api/user-personas/{persona_id}")
async def api_delete_user_persona(persona_id: int):
    success = await delete_user_persona(persona_id)
    if not success:
        raise HTTPException(404, "User persona not found")
    return {"ok": True}


# Reset ──


class ResetConfirm(BaseModel):
    confirm: bool


@app.post("/api/reset")
async def api_reset(data: ResetConfirm):
    """Reset mood_fragments, director_fragments, phrase_bank, and settings to defaults."""
    if not data.confirm:
        raise HTTPException(400, "Confirmation required")
    await reset_to_defaults()
    return {"ok": True}


# Conversations ──


@app.get("/api/conversations")
async def api_list_conversations():
    return await list_conversations()


@app.post("/api/conversations")
async def api_create_conversation(data: ConversationCreate):
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
        msg_id = await add_message(
            cid, "assistant", first_mes.strip(), 0, attachments=None
        )
        await set_active_leaf(cid, msg_id)

        # If we have a character card with alternate greetings, create swipe versions
        if card_id:
            card = await get_character_card(card_id)
            if card:
                alternate_greetings = card.get("alternate_greetings", [])
                count = await insert_alternate_greeting_swipes(cid, alternate_greetings)
                if count:
                    logger.info(
                        f"Created {count} alternate greeting swipes for conversation {cid}"
                    )

    return conv


@app.delete("/api/conversations/{cid}")
async def api_delete_conversation(cid: str):
    if not await delete_conversation(cid):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


@app.post("/api/conversations/{cid}/touch")
async def api_touch_conversation(cid: str):
    if not await touch_conversation(cid):
        raise HTTPException(404, "Conversation not found")
    return {"ok": True}


# Character Cards ──


@app.get("/api/characters")
async def api_list_characters():
    return await list_character_cards()


@app.post("/api/characters")
async def api_create_character(data: CharacterCardCreate):
    card_data = data.model_dump()
    card_data["id"] = card_data.get("id") or str(
        uuid.uuid4()
    )  # see CharacterCardCreate
    card_data["source_format"] = card_data.get("source_format") or "manual"
    try:
        return await create_character_card(card_data)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@app.post("/api/characters/import")
async def api_import_character(file: Annotated[UploadFile, File(...)]):
    """Import a SillyTavern-compatible character card PNG."""
    if not file.filename or not file.filename.lower().endswith(".png"):
        raise HTTPException(400, "Only .png character card files are supported")

    # Save to temp file for the parser
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Check for an embedded orb_id (card exported from this app) first so
        # that re-importing a previously exported card relinks conversation history.
        orb_id = tavern_cards.read_orb_id(tmp_path)
        card = tavern_cards.parse(tmp_path)
        card_dict = tavern_cards.card_to_dict(card)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse tavern card")
        raise HTTPException(400, f"Failed to parse character card: {e}") from e
    finally:
        os.unlink(tmp_path)

    # Determine stable card ID: prefer the embedded orb_id, fall back to SHA-256
    # of the raw PNG bytes so that reimporting the exact same file is idempotent.
    if orb_id:
        card_id = orb_id
    else:
        card_id = str(uuid.UUID(bytes=hashlib.sha256(content).digest()[:16], version=5))

    # Store the full PNG as the avatar
    avatar_b64 = base64.b64encode(content).decode("ascii")
    avatar_mime = "image/png"

    card_dict["id"] = card_id
    card_dict["avatar_b64"] = avatar_b64
    card_dict["avatar_mime"] = avatar_mime

    return card_dict


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
    await sync_conversations_for_card(card_id, result)
    return result


@app.delete("/api/characters/{card_id}")
async def api_delete_character(card_id: str, delete_conversations: bool = False):
    if not await delete_character_card(card_id, delete_conversations):
        raise HTTPException(404, "Character card not found")
    return {"ok": True}


@app.get("/api/characters/{card_id}/avatar")
async def api_get_avatar(card_id: str):
    result = await get_character_avatar(card_id)
    if not result:
        raise HTTPException(404, "No avatar found")
    image_bytes, mime_type = result
    return Response(content=image_bytes, media_type=mime_type or "image/png")


@app.get("/api/characters/{card_id}/export")
async def api_export_character(card_id: str):
    """Export a character card as a SillyTavern V2-compatible PNG."""
    card = await get_character_card(card_id, include_avatar=True)
    if not card:
        raise HTTPException(404, "Character not found")

    avatar_bytes: bytes | None = None
    if card.get("avatar_b64"):
        try:
            avatar_bytes = base64.b64decode(card["avatar_b64"])
        except Exception:
            logger.warning(
                "Avatar data for card %s is corrupt; exporting without avatar", card_id
            )
            avatar_bytes = None

    card["id"] = card_id
    png_bytes = tavern_cards.to_png(card, avatar_bytes)

    safe_name = (
        "".join(
            c for c in card.get("name", "character") if c.isalnum() or c in " _-"
        ).strip()
        or "character"
    )
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.png"'},
    )


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


async def _sse_stream(
    gen, request: Request, *, client_ref: list | None = None, cid: str | None = None
):
    """Wrap an event-dict async generator as SSE, stopping cleanly on client disconnect.

    The primary stop path is the explicit POST /stop endpoint, which calls
    LLMClient.abort() directly. That in turn breaks out of the asyncio.wait()
    loop in complete() and lets the async-with block close the TCP connection
    to the LLM server normally — no task cancellation needed.

    A background watcher also polls request.is_disconnected() as a fallback
    for cases like the user closing the browser tab without clicking Stop.
    """
    if cid and client_ref:
        # Will be populated after the first __anext__() drives handle_turn past
        # _load_pipeline_context; register as soon as it appears.
        pass  # registration happens inside the loop below

    async def _watch_disconnect() -> None:
        try:
            while True:
                if await request.is_disconnected():
                    if client_ref:
                        client_ref[0].abort()
                    return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            pass

    watcher = asyncio.create_task(_watch_disconnect())
    try:
        async for event in gen:
            # Register client in _active_clients on the first event (by which
            # point handle_turn has already populated client_ref).
            if cid and client_ref and cid not in _active_clients:
                _active_clients[cid] = client_ref[0]
            evt_type = event["event"]
            evt_data = event.get("data", "")
            if isinstance(evt_data, dict):
                evt_data = json.dumps(evt_data)
            elif isinstance(evt_data, str):
                evt_data = evt_data.replace("\n", "\\n")
            yield f"event: {evt_type}\ndata: {evt_data}\n\n"
    finally:
        if cid:
            _active_clients.pop(cid, None)
        watcher.cancel()
        # Shield aclose() from CancelledError so the orchestrator's finally
        # block (fallback persistence of incomplete messages) always runs.
        try:
            await asyncio.shield(gen.aclose())
        except asyncio.CancelledError:
            try:
                await gen.aclose()
            except Exception:
                pass


@app.post("/api/conversations/{cid}/stop")
async def api_stop_generation(cid: str):
    """Abort the active LLM generation for this conversation, if any."""
    client = _active_clients.get(cid)
    if client:
        client.abort()
        logger.info("Stop requested for conversation %s — aborted", cid)
    return {"ok": True}


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

    # For edits with no regeneration, just update the content in-place
    if not data.regenerate:
        await update_message_content(msg_id, data.content)
        return {"ok": True}

    # Copy attachments from original message (if any)
    original_attachments = await get_attachments_for_message(msg_id)
    attachments = []
    for att in original_attachments:
        attachments.append(
            {
                "mime_type": att["mime_type"],
                "data_b64": att["data_b64"],
                "filename": att["filename"],
                "size": att["size"],
            }
        )

    # Create sibling (same parent_id as original)
    new_msg_id = await add_message(
        cid,
        original["role"],
        data.content,
        original["turn_index"],
        parent_id=original.get("parent_id"),
        attachments=attachments if attachments else None,
    )
    await set_active_leaf(cid, new_msg_id)

    should_stream_regen = original["role"] == "user" and data.regenerate

    if should_stream_regen:
        client_ref: list = []
        return _CleanupStreamingResponse(
            _sse_stream(
                handle_turn(
                    cid,
                    data.content,
                    skip_user_persist=True,
                    attachments=attachments,
                    client_ref=client_ref,
                ),
                request,
                client_ref=client_ref,
                cid=cid,
            ),
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
async def api_regenerate_msg(
    cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None
):
    """Regenerate a specific assistant message as a new sibling branch."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    client_ref: list = []
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_regenerate(cid, msg_id, client_ref=client_ref),
            request,
            client_ref=client_ref,
            cid=cid,
        ),
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


# Chat (SSE streaming) ──


@app.post("/api/conversations/{cid}/send")
async def api_send_message(cid: str, data: SendMessage, request: Request):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    attachments = [a.dict() for a in data.attachments]
    client_ref: list = []
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_turn(
                cid, data.content, attachments=attachments, client_ref=client_ref
            ),
            request,
            client_ref=client_ref,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


@app.post("/api/conversations/{cid}/continue")
async def api_continue_from_user(cid: str, request: Request, data: Optional[RegenerateMsg] = None):
    """Generate an assistant response for the current user turn without creating a new message."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")
    messages = await get_messages(cid)
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(400, "Last message is not a user message")
    user_content = messages[-1]["content"]
    client_ref: list = []
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_turn(cid, user_content, skip_user_persist=True, client_ref=client_ref),
            request,
            client_ref=client_ref,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


# Frontend serving ──


@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# Mount static files last
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
