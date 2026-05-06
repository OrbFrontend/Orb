from __future__ import annotations
import hashlib
import json
import uuid
import logging
import base64
import tempfile
from contextlib import asynccontextmanager

from typing import Annotated, Any, AsyncGenerator, Optional, List, cast
from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import os

from .migrations import run_pending
from .database import (
    DB_PATH,
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
    update_conversation,
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
    get_worlds,
    get_world,
    get_world_by_name,
    create_world,
    update_world,
    delete_world,
    get_lorebook_entries,
    get_lorebook_entry,
    create_lorebook_entry,
    update_lorebook_entry,
    delete_lorebook_entry,
    get_active_lorebook_entries,
    get_db,
    resolve_char_context,
    get_user_persona,
    get_voice_profile,
    upsert_voice_profile,
)
import asyncio
from .orchestrator import (
    handle_turn,
    handle_regenerate,
    handle_super_regenerate,
    handle_magic_rewrite,
)
from .llm_client import LLMClient
from .endpoint_profiles import profile_for
from . import tavern_cards
from . import prompt_builder
from .tts import get_adapter, list_backends
from .tts.speech_scripter import run_speech_scripter

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend"
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    run_pending(DB_PATH)
    await init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="Orb", lifespan=lifespan)

# Active LLM generations keyed by conversation ID.
# Populated when streaming starts; cleared when it ends or is aborted.
_active_clients: dict[str, list[LLMClient]] = {}


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
    shared_system_prompt: Optional[str] = None
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
    show_editor_diff: Optional[bool] = None
    hide_streaming_until_baked: Optional[bool] = None
    agent_same_as_writer: Optional[bool] = None
    agent_endpoint_id: Optional[int] = None
    agent_shared_system_prompt: Optional[str] = None


class EndpointCreate(BaseModel):
    url: str
    api_key: str = ""


class EndpointUpdate(BaseModel):
    url: Optional[str] = None
    api_key: Optional[str] = None
    active_model_config_id: Optional[int] = None
    agent_active_model_config_id: Optional[int] = None


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
    role: str = "writer"


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


class WorldCreate(BaseModel):
    name: str


class WorldUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None


class LorebookEntryCreate(BaseModel):
    name: str
    content: str = ""
    keywords: list[str] = []
    case_insensitive: bool = True
    priority: int = 100
    enabled: bool = True


class LorebookEntryUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    keywords: Optional[list[str]] = None
    case_insensitive: Optional[bool] = None
    priority: Optional[int] = None
    enabled: Optional[bool] = None


class LorebookImportPayload(BaseModel):
    # Accepts raw lorebook JSON as parsed by the frontend.
    # Supports two common formats:
    #   - SillyTavern standalone lorebook: {"entries": {"0": {...}, "1": {...}}}
    #     where each entry has `key` (list), `comment`, `content`, `disable`, `order`, `caseSensitive`
    #   - Tavern V2 character_book: {"entries": [...]}
    #     where each entry has `keys`, `name`, `content`, `enabled`, `insertion_order`, `case_sensitive`
    entries: Any


class ConversationCreate(BaseModel):
    title: str = "New Conversation"
    character_card_id: Optional[str] = None
    character_name: str = ""
    character_scenario: str = ""
    first_mes: str = ""
    post_history_instructions: str = ""


class ConversationUpdate(BaseModel):
    title: Optional[str] = None


class SummarizeRequest(BaseModel):
    keep_count: int  # must be one of 2, 4, 6, 8


class CompressRequest(BaseModel):
    summary: str
    keep_count: int  # must be one of 2, 4, 6, 8


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
    world_id: Optional[str] = None
    character_book: Optional[dict] = None


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
    world_id: Optional[str] = None


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


class MagicRewriteMsg(BaseModel):
    direction: str


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


@app.get("/api/themes")
async def api_get_themes():
    themes_dir = os.path.join(FRONTEND_DIR, "themes")
    names = sorted(f[:-4] for f in os.listdir(themes_dir) if f.endswith(".css"))
    return {"themes": names}


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
        raise HTTPException(status_code=404, detail="Endpoint not found")
    return result


@app.delete("/api/endpoints/{endpoint_id}")
async def api_delete_endpoint(endpoint_id: int):
    if not await delete_endpoint(endpoint_id):
        raise HTTPException(status_code=404, detail="Endpoint not found")
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
            raise HTTPException(status_code=404, detail="Endpoint not found")
        raise


@app.put("/api/models/{config_id}")
async def api_update_model_config(config_id: int, data: ModelConfigUpdate):
    result = await update_model_config(config_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="Model config not found")
    return result


@app.delete("/api/models/{config_id}")
async def api_delete_model_config(config_id: int):
    if not await delete_model_config(config_id):
        raise HTTPException(status_code=404, detail="Model config not found")
    return {"ok": True}


# Mood Fragments ──


@app.get("/api/fragments")
async def api_list_mood_fragments():
    return await get_mood_fragments()


@app.post("/api/fragments")
async def api_create_mood_fragment(data: MoodFragmentCreate):
    existing = await get_mood_fragment(data.id)
    if existing:
        raise HTTPException(
            status_code=400, detail="Mood fragment with this ID already exists"
        )
    return await create_mood_fragment(data.model_dump())


@app.put("/api/fragments/{fid}")
async def api_update_mood_fragment(fid: str, data: MoodFragmentUpdate):
    result = await update_mood_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=404, detail="Mood fragment not found")
    return result


@app.delete("/api/fragments/{fid}")
async def api_delete_mood_fragment(fid: str):
    if not await delete_mood_fragment(fid):
        raise HTTPException(
            status_code=404, detail="Mood fragment not found or is built-in"
        )
    return {"ok": True}


# Director Fragments ──


@app.get("/api/director-fragments")
async def api_list_director_fragments():
    return await get_director_fragments()


@app.post("/api/director-fragments")
async def api_create_director_fragment(data: DirectorFragmentCreate):
    existing = await get_director_fragment(data.id)
    if existing:
        raise HTTPException(
            status_code=400, detail="Director fragment with this ID already exists"
        )
    result = await create_director_fragment(data.model_dump())
    if not result:
        raise HTTPException(
            status_code=500, detail="Failed to create director fragment"
        )
    return result


@app.put("/api/director-fragments/{fid}")
async def api_update_director_fragment(fid: str, data: DirectorFragmentUpdate):
    result = await update_director_fragment(fid, data.model_dump(exclude_none=True))
    if not result:
        raise HTTPException(status_code=404, detail="Director fragment not found")
    return result


@app.delete("/api/director-fragments/{fid}")
async def api_delete_director_fragment(fid: str):
    if not await delete_director_fragment(fid):
        raise HTTPException(status_code=404, detail="Director fragment not found")
    return {"ok": True}


# Worlds ──


@app.get("/api/worlds")
async def api_list_worlds():
    return await get_worlds()


@app.post("/api/worlds")
async def api_create_world(data: WorldCreate):
    return await create_world(data.model_dump())


@app.put("/api/worlds/{world_id}")
async def api_update_world(world_id: str, data: WorldUpdate):
    result = await update_world(world_id, data.model_dump(exclude_unset=True))
    if not result:
        raise HTTPException(status_code=404, detail="World not found")
    return result


@app.delete("/api/worlds/{world_id}")
async def api_delete_world(world_id: str):
    if not await delete_world(world_id):
        raise HTTPException(status_code=404, detail="World not found")
    return {"ok": True}


# Lorebook Entries ──


async def require_world(world_id: str) -> dict:
    world = await get_world(world_id)
    if not world:
        raise HTTPException(status_code=404, detail="World not found")
    return world


async def require_lorebook_entry(
    entry_id: int, world: dict = Depends(require_world)  # noqa: B008
) -> dict:
    entry = await get_lorebook_entry(entry_id)
    if not entry or entry.get("world_id") != world["id"]:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


@app.get("/api/worlds/{world_id}/entries")
async def api_list_lorebook_entries(world: dict = Depends(require_world)):  # noqa: B008
    return await get_lorebook_entries(world["id"])


@app.post("/api/worlds/{world_id}/entries")
async def api_create_lorebook_entry(
    data: LorebookEntryCreate, world: dict = Depends(require_world)  # noqa: B008
):
    return await create_lorebook_entry(world["id"], data.model_dump())


@app.get("/api/worlds/{world_id}/entries/{entry_id}")
async def api_get_lorebook_entry(
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    return entry


@app.put("/api/worlds/{world_id}/entries/{entry_id}")
async def api_update_lorebook_entry(
    data: LorebookEntryUpdate,
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    result = await update_lorebook_entry(
        entry["id"], data.model_dump(exclude_unset=True)
    )
    if not result:
        raise HTTPException(status_code=404, detail="Entry not found")
    return result


@app.delete("/api/worlds/{world_id}/entries/{entry_id}")
async def api_delete_lorebook_entry(
    entry: dict = Depends(require_lorebook_entry),  # noqa: B008
):
    if not await delete_lorebook_entry(entry["id"]):
        raise HTTPException(status_code=404, detail="Entry not found")
    return {"ok": True}


def _normalise_lorebook_entry(item: dict) -> dict:
    keywords = item.get("keys") or item.get("key") or []
    if not isinstance(keywords, list):
        keywords = []
    keywords = [str(k) for k in keywords if k]
    name = item.get("name") or item.get("comment") or ""
    if "disable" in item:
        enabled = not item["disable"]
    else:
        enabled = bool(item.get("enabled", True))
    priority = int(item.get("insertion_order") or item.get("order") or 100)
    case_sensitive = item.get("caseSensitive") or item.get("case_sensitive")
    return {
        "name": str(name),
        "content": str(item.get("content") or ""),
        "keywords": keywords,
        "enabled": enabled,
        "priority": priority,
        "case_insensitive": not bool(case_sensitive),
    }


@app.post("/api/worlds/{world_id}/import")
async def api_import_lorebook(world_id: str, payload: LorebookImportPayload):
    world = await get_world(world_id)
    if not world:
        raise HTTPException(status_code=404, detail="World not found")

    raw_entries = payload.entries
    # Normalise both formats into a flat list of dicts
    if isinstance(raw_entries, dict):
        # SillyTavern standalone: {"0": {...}, "1": {...}}
        items = list(raw_entries.values())
    elif isinstance(raw_entries, list):
        # Tavern V2 character_book: [...]
        items = raw_entries
    else:
        raise HTTPException(
            status_code=422, detail="entries must be an object or array"
        )

    created = []
    for item in items:
        if not isinstance(item, dict):
            continue
        entry_data = _normalise_lorebook_entry(item)
        created.append(await create_lorebook_entry(world_id, entry_data))

    return {"imported": len(created), "entries": created}


@app.get("/api/lorebook-entries/active")
async def api_get_active_lorebook_entries():
    return await get_active_lorebook_entries()


# Phrase Bank ──


@app.get("/api/phrase-bank")
async def api_get_phrase_bank():
    """Return phrase bank rows with ids for UI management."""
    return await get_phrase_bank_rows()


@app.post("/api/phrase-bank")
async def api_create_phrase_group(data: PhraseGroupCreate):
    """Create a new phrase variant group."""
    if not data.variants or len(data.variants) == 0:
        raise HTTPException(status_code=400, detail="At least one variant is required")
    # Validate all variants are strings
    for v in data.variants:
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(
                status_code=400, detail="All variants must be non-empty strings"
            )
    group_id = await add_phrase_group(data.variants)
    return {"id": group_id, "variants": data.variants}


@app.put("/api/phrase-bank/{group_id}")
async def api_update_phrase_group(group_id: int, data: PhraseGroupUpdate):
    """Update an existing phrase variant group."""
    if not data.variants or len(data.variants) == 0:
        raise HTTPException(status_code=400, detail="At least one variant is required")
    # Validate all variants are strings
    for v in data.variants:
        if not isinstance(v, str) or not v.strip():
            raise HTTPException(
                status_code=400, detail="All variants must be non-empty strings"
            )
    success = await update_phrase_group(group_id, data.variants)
    if not success:
        raise HTTPException(status_code=404, detail="Phrase group not found")
    return {"ok": True, "id": group_id, "variants": data.variants}


@app.delete("/api/phrase-bank/{group_id}")
async def api_delete_phrase_group(group_id: int):
    """Delete a phrase variant group."""
    success = await delete_phrase_group(group_id)
    if not success:
        raise HTTPException(status_code=404, detail="Phrase group not found")
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
        raise HTTPException(status_code=404, detail="User persona not found")
    return result


@app.delete("/api/user-personas/{persona_id}")
async def api_delete_user_persona(persona_id: int):
    success = await delete_user_persona(persona_id)
    if not success:
        raise HTTPException(status_code=404, detail="User persona not found")
    return {"ok": True}


# Reset ──


class ResetConfirm(BaseModel):
    confirm: bool


@app.post("/api/reset")
async def api_reset(data: ResetConfirm):
    """Reset mood_fragments, director_fragments, phrase_bank, and settings to defaults."""
    if not data.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
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
            raise HTTPException(status_code=404, detail="Character card not found")
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
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True}


@app.post("/api/conversations/{cid}/touch")
async def api_touch_conversation(cid: str):
    if not await touch_conversation(cid):
        raise HTTPException(status_code=404, detail="Conversation not found")
    return {"ok": True}


@app.put("/api/conversations/{cid}")
async def api_update_conversation(cid: str, data: ConversationUpdate):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    result = await update_conversation(cid, data.model_dump(exclude_unset=True))
    return result


@app.post("/api/conversations/{cid}/summarize")
async def api_summarize_conversation(
    cid: str, data: SummarizeRequest, request: Request
):
    """Stream a narrative summary of the conversation history, excluding the last keep_count messages."""
    if data.keep_count not in (2, 4, 6, 8):
        raise HTTPException(
            status_code=400, detail="keep_count must be one of 2, 4, 6, 8"
        )

    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = await get_messages_with_branch_info(cid)
    history_slice = messages[: max(0, len(messages) - data.keep_count)]

    if not history_slice:
        raise HTTPException(status_code=400, detail="Not enough messages to summarize")

    settings = await get_settings()
    char_name = conv.get("character_name", "Character") or "Character"
    char_scenario = conv.get("character_scenario", "") or ""

    client = LLMClient(
        settings["endpoint_url"],
        api_key=settings.get("api_key", ""),
        profile=profile_for(settings["endpoint_url"], settings.get("model_name", "")),
    )
    client_ref = [client]

    async def _gen():
        parts = []
        for msg in history_slice:
            label = char_name if msg["role"] == "assistant" else "User"
            parts.append(f"{label}: {msg['content']}")
        history_text = "\n\n".join(parts)

        system = (
            f"You are a narrative scribe summarizing a roleplay story between User and {char_name}."
            + (f" Setting: {char_scenario}." if char_scenario else "")
            + " Write a rich prose summary of the events so far."
            " Preserve significant dialogue verbatim in quotes."
            " Record key story beats, milestones, and relationship developments."
            " Write in past tense. Be thorough — this will be the sole context for the story's continuation."
        )

        llm_messages = [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": f"Story to summarize:\n\n{history_text}\n\nWrite the narrative summary now.",
            },
        ]

        params = {
            k: v
            for k in [
                "temperature",
                "max_tokens",
                "top_p",
                "min_p",
                "top_k",
                "repetition_penalty",
            ]
            if (v := settings.get(k)) is not None
        }

        try:
            async for chunk in client.complete(
                llm_messages, settings.get("model_name", ""), **params
            ):
                if chunk["type"] == "content":
                    yield {"event": "token", "data": chunk["delta"]}
            yield {"event": "done", "data": ""}
        except Exception as e:
            logger.error("Summarize error: %s", e)
            yield {"event": "error", "data": str(e)}

    return _CleanupStreamingResponse(
        _sse_stream(_gen(), request, client_ref=client_ref, cid=cid),
        media_type="text/event-stream",
    )


@app.post("/api/conversations/{cid}/compress")
async def api_compress_conversation(cid: str, data: CompressRequest):
    """Create a new conversation seeded with a summary, then re-append the last keep_count messages."""
    if data.keep_count not in (2, 4, 6, 8):
        raise HTTPException(
            status_code=400, detail="keep_count must be one of 2, 4, 6, 8"
        )
    if not data.summary.strip():
        raise HTTPException(status_code=400, detail="summary must not be empty")

    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = await get_messages_with_branch_info(cid)
    tail = messages[max(0, len(messages) - data.keep_count) :]

    char_name = conv.get("character_name", "") or ""
    old_title = conv.get("title", "") or ""
    new_title = f"{old_title} (continued)" if old_title else "Continued"
    new_cid = str(uuid.uuid4())

    await create_conversation(
        cid=new_cid,
        title=new_title,
        char_name=char_name,
        char_scenario=conv.get("character_scenario", "") or "",
        first_mes="",
        post_history_instructions=conv.get("post_history_instructions", "") or "",
        character_card_id=conv.get("character_card_id"),
    )

    # Seed with the approved summary as the first assistant message
    prev_id = await add_message(new_cid, "assistant", data.summary.strip(), 0)
    await set_active_leaf(new_cid, prev_id)

    # Re-insert tail messages, chaining via parent_id
    for i, msg in enumerate(tail):
        atts = msg.get("attachments") or []
        att_list = (
            [
                {
                    "mime_type": a["mime_type"],
                    "data_b64": a["data_b64"],
                    "filename": a.get("filename"),
                    "size": a.get("size"),
                }
                for a in atts
            ]
            if atts
            else None
        )
        prev_id = await add_message(
            new_cid,
            msg["role"],
            msg["content"],
            i + 1,
            parent_id=prev_id,
            attachments=att_list,
        )
        await set_active_leaf(new_cid, prev_id)

    return {"new_conversation_id": new_cid}


# Character Cards ──


@app.get("/api/characters")
async def api_list_characters():
    return await list_character_cards()


@app.post("/api/characters")
async def api_create_character(data: CharacterCardCreate):
    card_data = data.model_dump()
    card_data["id"] = card_data.get("id") or str(uuid.uuid4())
    card_data["source_format"] = card_data.get("source_format") or "manual"

    character_book = card_data.pop("character_book", None)
    if character_book and not card_data.get("world_id"):
        entries = character_book.get("entries") or []
        if isinstance(entries, dict):
            entries = list(entries.values())
        if entries:
            book_name = character_book.get("name") or card_data["name"]
            world = await get_world_by_name(book_name)
            if not world:
                world = await create_world({"name": book_name})
                for item in entries:
                    if isinstance(item, dict):
                        await create_lorebook_entry(
                            world["id"], _normalise_lorebook_entry(item)
                        )
            card_data["world_id"] = world["id"]

    try:
        return await create_character_card(card_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/characters/import")
async def api_import_character(file: Annotated[UploadFile, File(...)]):
    """Import a SillyTavern-compatible character card PNG."""
    if not file.filename or not file.filename.lower().endswith(".png"):
        raise HTTPException(
            status_code=400, detail="Only .png character card files are supported"
        )

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
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to parse tavern card")
        raise HTTPException(
            status_code=400, detail=f"Failed to parse character card: {e}"
        ) from e
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
        raise HTTPException(status_code=404, detail="Character card not found")
    return card


@app.put("/api/characters/{card_id}")
async def api_update_character(card_id: str, data: CharacterCardUpdate):
    old_card = await get_character_card(card_id)
    update_data = data.model_dump(exclude_none=True)
    # world_id can be explicitly set to None to unlink; preserve it via model_fields_set
    if "world_id" in data.model_fields_set:
        update_data["world_id"] = data.world_id
    result = await update_character_card(card_id, update_data)
    if not result:
        raise HTTPException(status_code=404, detail="Character card not found")
    old_name = old_card["name"] if old_card and "name" in update_data else None
    await sync_conversations_for_card(card_id, result, old_name=old_name)
    return result


@app.delete("/api/characters/{card_id}")
async def api_delete_character(card_id: str, delete_conversations: bool = False):
    if not await delete_character_card(card_id, delete_conversations):
        raise HTTPException(status_code=404, detail="Character card not found")
    return {"ok": True}


@app.get("/api/characters/{card_id}/avatar")
async def api_get_avatar(card_id: str):
    result = await get_character_avatar(card_id)
    if not result:
        raise HTTPException(status_code=404, detail="No avatar found")
    image_bytes, mime_type = result
    return Response(content=image_bytes, media_type=mime_type or "image/png")


@app.get("/api/characters/{card_id}/export")
async def api_export_character(card_id: str):
    """Export a character card as a SillyTavern V2-compatible PNG."""
    card = await get_character_card(card_id, include_avatar=True)
    if not card:
        raise HTTPException(status_code=404, detail="Character not found")

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

    # If the character is linked to a lorebook, embed it as character_book
    if card.get("world_id") and not card.get("character_book"):
        world = await get_world(card["world_id"])
        entries = await get_lorebook_entries(card["world_id"])
        card["character_book"] = {
            "name": world["name"] if world else "",
            "extensions": {},
            "entries": [
                {
                    "keys": e["keywords"],
                    "content": e["content"],
                    "extensions": {},
                    "enabled": bool(e["enabled"]),
                    "insertion_order": e["sort_order"],
                    "case_sensitive": not bool(e["case_insensitive"]),
                    "name": e["name"],
                    "priority": e["priority"],
                    "id": e["id"],
                }
                for e in entries
            ],
        }

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
                gen = cast(AsyncGenerator[Any, None], self.body_iterator)
                try:
                    await asyncio.shield(gen.aclose())
                except asyncio.CancelledError:
                    # Shield was cancelled; try once more
                    try:
                        await gen.aclose()
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
                        for c in client_ref:
                            c.abort()
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
                _active_clients[cid] = list(client_ref)
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
    clients = _active_clients.get(cid)
    if clients:
        for c in clients:
            c.abort()
        logger.info("Stop requested for conversation %s — aborted", cid)
    return {"ok": True}


@app.get("/api/conversations/{cid}/messages")
async def api_get_messages(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/edit")
async def api_edit_message(cid: str, msg_id: int, data: EditMessage, request: Request):
    """Edit a message by creating a sibling branch. Old branches are preserved.
    If editing a user message and regenerate=True, streams a new assistant response."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    original = await get_message_by_id(msg_id)
    if not original or original["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found")

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
        raise HTTPException(status_code=404, detail="Conversation not found")
    if not await delete_message_with_descendants(cid, msg_id):
        raise HTTPException(status_code=404, detail="Message not found")
    return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/switch-branch")
async def api_switch_branch(cid: str, msg_id: int):
    """Switch to the branch containing msg_id (sets active leaf to deepest descendant)."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    success = await switch_to_branch(cid, msg_id)
    if not success:
        raise HTTPException(status_code=404, detail="Message not found")
    return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/regenerate")
async def api_regenerate_msg(
    cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None
):
    """Regenerate a specific assistant message as a new sibling branch."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

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


@app.post("/api/conversations/{cid}/messages/{msg_id}/super_regenerate")
async def api_super_regenerate_msg(
    cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None
):
    """Super-regenerate: keeps prior response as context, asks model for a different direction."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    client_ref: list = []
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_super_regenerate(cid, msg_id, client_ref=client_ref),
            request,
            client_ref=client_ref,
            cid=cid,
        ),
        media_type="text/event-stream",
    )


@app.post("/api/conversations/{cid}/messages/{msg_id}/magic_rewrite")
async def api_magic_rewrite_msg(
    cid: str, msg_id: int, request: Request, data: MagicRewriteMsg
):
    """Magic rewrite: calls the LLM directly with a user-supplied direction, no agent passes."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    client_ref: list = []
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_magic_rewrite(cid, msg_id, data.direction, client_ref=client_ref),
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
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await get_director_state(cid)


@app.get("/api/conversations/{cid}/logs")
async def api_get_logs(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return await get_conversation_logs(cid)


@app.get("/api/conversations/{cid}/context-size")
async def api_get_context_size(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    settings = await get_settings()
    messages = await get_messages(cid)
    director = await get_director_state(cid) or {}
    director_frags = [
        f for f in await get_director_fragments() if f.get("enabled", True)
    ]
    mood_frags = [f for f in await get_mood_fragments() if f.get("enabled", True)]
    lorebook_entries = await get_active_lorebook_entries()

    # Resolve persona
    persona_id = settings.get("active_persona_id")
    active_persona = await get_user_persona(persona_id) if persona_id else None
    user_name = (
        active_persona["name"] if active_persona else settings.get("user_name", "User")
    )
    user_desc = (
        active_persona.get("description", "")
        if active_persona
        else settings.get("user_description", "")
    )

    # Resolve character context
    system_prompt, char_persona, mes_example = await resolve_char_context(
        conv, settings
    )

    def _resolve(text):
        return prompt_builder.replace_placeholders(
            text, user_name, conv["character_name"]
        )

    # Measure each component individually
    sys_text = system_prompt or ""
    persona_text = _resolve(char_persona or "")
    scenario_text = _resolve(conv.get("character_scenario", "") or "")
    mes_text = _resolve(mes_example or "")
    post_text = _resolve(conv.get("post_history_instructions", "") or "")
    user_persona_text = (
        f"## User: {user_name}\n{_resolve(user_desc)}" if user_desc else ""
    )
    msg_chars = sum(len(m.get("content", "") or "") for m in messages)

    # Director injection
    active_moods = director.get("active_moods", []) if director else []
    inj_block = prompt_builder.compute_style_injection_block(
        active_moods,
        active_moods,
        mood_frags,
        director_frags,
        bool(settings.get("enable_agent", 1)),
        {},
    )

    # Lorebook injection
    lorebook_block = prompt_builder.compute_lorebook_injection_block(
        lorebook_entries, messages[-6:] if len(messages) >= 6 else messages
    )

    def est(chars):
        return max(1, round(chars / 3.5))

    breakdown = {}
    for label, chars in [
        ("system_prompt", len(sys_text)),
        ("char_persona", len(persona_text)),
        ("scenario", len(scenario_text)),
        ("mes_example", len(mes_text)),
        ("user_persona", len(user_persona_text)),
        ("messages", msg_chars),
        ("post_history", len(post_text)),
        ("director_injection", len(inj_block)),
        ("lorebook", len(lorebook_block)),
    ]:
        breakdown[label] = {"chars": chars, "tokens_est": est(chars)}

    total_chars = sum(v["chars"] for v in breakdown.values())
    return {
        "total_chars": total_chars,
        "total_tokens_est": est(total_chars),
        "breakdown": breakdown,
        "message_count": len(messages),
    }


# Chat (SSE streaming) ──


@app.post("/api/conversations/{cid}/send")
async def api_send_message(cid: str, data: SendMessage, request: Request):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

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
async def api_continue_from_user(
    cid: str, request: Request, data: Optional[RegenerateMsg] = None
):
    """Generate an assistant response for the current user turn without creating a new message."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await get_messages(cid)
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(
            status_code=400, detail="Last message is not a user message"
        )
    user_content = messages[-1]["content"]
    client_ref: list = []
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_turn(
                cid, user_content, skip_user_persist=True, client_ref=client_ref
            ),
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


TTS_CACHE_DIR = os.path.join(os.path.dirname(__file__), "data", "tts_cache")


def _tts_cache_media_type(profile: dict) -> tuple[str, str]:
    """Return the expected cached media type and extension for a voice profile."""
    if profile.get("backend") == "kokoro":
        return "audio/wav", "wav"
    return "audio/mpeg", "mp3"


def _tts_cache_path(cid: str, msg_id: int, profile: dict, content: str = "") -> str:
    """Cache path keyed by message content and voice/scripter configuration."""
    import hashlib
    media_type, ext = _tts_cache_media_type(profile)
    fingerprint = hashlib.md5(
        f"{profile.get('backend', '')}|{profile.get('voice_id', '')}|"
        f"{profile.get('language', '')}|{profile.get('rate', '')}|{profile.get('pitch', '')}|"
        f"{profile.get('scripter_model', '')}|{profile.get('scripter_temperature', '')}|"
        f"{profile.get('speech_prompt', '')}|{profile.get('api_url', '')}|"
        f"{profile.get('model', '')}|{media_type}|{content}"
        .encode()
    ).hexdigest()[:8]
    return os.path.join(TTS_CACHE_DIR, cid, f"{msg_id}_{fingerprint}.{ext}")


@app.get("/api/tts/backends")
async def api_tts_backends():
    """List available TTS backends."""
    return list_backends()


@app.get("/api/tts/voices")
async def api_tts_voices(
    backend: str = "edge", language: str = "", api_url: str = "", api_key: str = ""
):
    """List available voices for a TTS backend."""
    try:
        adapter = get_adapter(backend)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return await adapter.list_voices(language, api_url=api_url, api_key=api_key or None)


@app.get("/api/tts/models")
async def api_tts_models(backend: str = "", api_url: str = "", api_key: str = ""):
    """List available TTS models for a backend."""
    if not backend:
        return []
    try:
        adapter = get_adapter(backend)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not hasattr(adapter, "list_models"):
        return []
    return await adapter.list_models(api_url=api_url, api_key=api_key or None)


@app.post("/api/tts/preview")
async def api_tts_preview(body: dict):
    """Quick TTS preview — synthesize short text without conversation context."""
    text = body.get("text", "Preview.")
    backend = body.get("backend", "edge")
    voice_id = body.get("voice_id", "en-US-JennyNeural")
    try:
        adapter = get_adapter(backend)
    except ValueError as e:
        raise HTTPException(400, str(e))
    from .tts.base import SpeakableChunk

    chunk = SpeakableChunk(text=text, emotion="neutral")
    result = await adapter.synthesize(
        [chunk],
        voice_id=voice_id,
        rate=1.0,
        pitch=1.0,
        api_url=body.get("api_url", ""),
        api_key=body.get("api_key", "") or None,
        model=body.get("model", ""),
    )
    return Response(content=result.audio_bytes, media_type=result.content_type)


@app.get("/api/characters/{card_id}/voice-profile")
async def api_get_voice_profile(card_id: str):
    """Get the TTS voice profile for a character."""
    profile = await get_voice_profile(card_id)
    if not profile:
        return {"character_card_id": card_id, "enabled": 0}
    return profile


@app.put("/api/characters/{card_id}/voice-profile")
async def api_update_voice_profile(card_id: str, body: dict):
    """Create or update the TTS voice profile for a character."""
    # Verify character exists
    card = await get_character_card(card_id)
    if not card:
        raise HTTPException(404, "Character not found")

    # Clear generated TTS files for all conversations using this character.
    async with get_db() as _db:
        _rows = await _db.execute(
            "SELECT id FROM conversations WHERE character_card_id = ?",
            (card_id,),
        )
        _conv_ids = [r["id"] for r in await _rows.fetchall()]
    for _cid in _conv_ids:
        _cache_dir = os.path.join(TTS_CACHE_DIR, _cid)
        if os.path.isdir(_cache_dir):
            for _name in os.listdir(_cache_dir):
                _path = os.path.join(_cache_dir, _name)
                if os.path.isfile(_path):
                    os.remove(_path)

    profile = await upsert_voice_profile(card_id, body)
    return profile


@app.post("/api/conversations/{cid}/messages/{msg_id}/speak")
async def api_speak_message(cid: str, msg_id: int):
    """Generate TTS audio for a message. Returns MP3 audio."""
    # Validate message exists and belongs to conversation
    msg = await get_message_by_id(msg_id)
    if not msg or msg["conversation_id"] != cid:
        raise HTTPException(404, "Message not found")
    if msg["role"] != "assistant":
        raise HTTPException(400, "TTS is only available for assistant messages")

    # Get conversation and character
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(404, "Conversation not found")

    card_id = conv.get("character_card_id")
    if not card_id:
        raise HTTPException(400, "No character associated with this conversation")

    # Get voice profile
    profile = await get_voice_profile(card_id)
    if not profile or not profile.get("enabled"):
        raise HTTPException(400, "TTS not enabled for this character")

    # Check cache
    cache_path = _tts_cache_path(cid, msg_id, profile, msg["content"])
    if os.path.exists(cache_path):
        media_type, ext = _tts_cache_media_type(profile)
        return FileResponse(
            cache_path,
            media_type=media_type,
            filename=f"msg_{msg_id}.{ext}",
        )

    # Get TTS adapter
    try:
        adapter = get_adapter(profile["backend"])
    except ValueError as e:
        raise HTTPException(400, str(e))

    # Build LLM client for speech scripter
    settings = await get_settings()
    endpoint_id = profile.get("endpoint_id") or settings.get("active_endpoint_id")
    endpoint = await get_endpoint(endpoint_id) if endpoint_id else None
    if not endpoint:
        raise HTTPException(400, "No LLM endpoint configured for speech scripter")

    # Determine speech scripter model
    scripter_model = profile.get("scripter_model") or settings.get("model_name", "")
    if not scripter_model:
        raise HTTPException(400, "No model configured for speech scripter")

    client = LLMClient(
        base_url=endpoint["url"],
        api_key=endpoint.get("api_key", ""),
        profile=profile_for(endpoint["url"], scripter_model),
    )

    # Get Director moods for context
    logs = await get_conversation_logs(cid)
    moods = []
    if logs:
        last_log = logs[-1]
        moods = last_log.get("active_moods_after", []) if isinstance(last_log, dict) else []

    # Run speech scripter pass
    chunks = await run_speech_scripter(
        client=client,
        model=scripter_model,
        writer_text=msg["content"],
        backend_type=profile["backend"],
        moods=moods,
        custom_prompt=profile.get("speech_prompt", ""),
        temperature=profile.get("scripter_temperature", 0.3),
    )

    # Synthesize audio
    result = await adapter.synthesize(
        chunks=chunks,
        voice_id=profile.get("voice_id", "en-US-JennyNeural"),
        language=profile.get("language", "en-US"),
        rate=profile.get("rate", 1.0),
        pitch=profile.get("pitch", 1.0),
        api_url=profile.get("api_url", ""),
        api_key=profile.get("api_key", "") or None,
        model=profile.get("model", ""),
    )

    if not result.audio_bytes:
        raise HTTPException(500, "TTS synthesis produced no audio")

    # Cache the audio
    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
    with open(cache_path, "wb") as f:
        f.write(result.audio_bytes)

    return Response(
        content=result.audio_bytes,
        media_type=result.content_type,
        headers={"Content-Length": str(len(result.audio_bytes))},
    )


# Chat (SSE streaming) ──


@app.post("/api/conversations/{cid}/send")
async def api_send_message(cid: str, data: SendMessage, request: Request):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

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
async def api_continue_from_user(
    cid: str, request: Request, data: Optional[RegenerateMsg] = None
):
    """Generate an assistant response for the current user turn without creating a new message."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await get_messages(cid)
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(
            status_code=400, detail="Last message is not a user message"
        )
    user_content = messages[-1]["content"]
    client_ref: list = []
    return _CleanupStreamingResponse(
        _sse_stream(
            handle_turn(
                cid, user_content, skip_user_persist=True, client_ref=client_ref
            ),
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

