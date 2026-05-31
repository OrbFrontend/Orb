from __future__ import annotations
import asyncio
import hashlib
import json
import re
import uuid
import logging
import base64
import tempfile

from contextlib import asynccontextmanager

from typing import Annotated, Any, AsyncGenerator, Optional, List, cast
from fastapi import Body, Depends, FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import StreamingResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, field_validator
import os

from .database.migrations import run_pending
from .database import (
    DB_PATH,
    get_db,
    get_messages_before,
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
    get_director_log_for_message,
    list_character_cards,
    get_character_card,
    create_character_card,
    update_character_card,
    delete_character_card,
    get_character_avatar,
    sync_conversations_for_card,
    insert_alternate_greeting_swipes,
    add_message,
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
    resolve_char_context,
    get_user_persona,
    get_workflow_attachment_by_id,
)
from .workflows.attachment_cache import (
    delete_workflow_attachments,
    insert_workflow_attachment,
    insert_workflow_attachments,
    record_access,
    rehydrate_attachment,
    set_active_sibling,
    validate_workflow_attachment_shape,
    EVICTED_MARKER,
    OVERSIZE_NO_METADATA_REASON,
    RehydrateAlreadyDoneError,
)
from .endpoint_profiles import profile_for
from .workflows import (
    HookType,
    OnDemandCtx,
    RegenCtx,
    RerollGenCtx,
    _readonly,
    get_subscription,
    get_workflow,
    get_workflow_config,
    list_workflows,
    set_workflow_config,
)
from .locks import workflow_character_state_lock, workflow_config_lock, workflow_state_lock
from .orchestrator import (
    handle_turn,
    handle_regenerate,
    handle_super_regenerate,
    handle_magic_rewrite,
)
from .llm_client import LLMClient
from .macros import Macros
from . import tavern_cards
from . import card_downloader
from . import prompt_builder
from .summarizer import ConversationSummarizer

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")


# Per-root_id serialization for mutations of workflow_attachments groups.
# Regenerate, reroll-gen, and activate all write the root's
# active_sibling_id. BEGIN IMMEDIATE prevents data corruption, but commit
# order across concurrent transactions is indeterminate; the loser's API
# response can name a sibling whose active-pointer status the winner has
# already overwritten. The lock turns concurrent requests into sequential
# ones so the loser proceeds against post-winner state.
#
# Dict grows over the process lifetime, bounded by distinct root_ids the
# user has interacted with. Single-user localhost app, so cap is small
# and process restart resets. dict.setdefault is a single CPython
# bytecode with no await between read and write, so no guard lock is
# needed around the dict itself.
_workflow_root_locks: dict[int, asyncio.Lock] = {}


@asynccontextmanager
async def _workflow_root_lock(root_id: int):
    lock = _workflow_root_locks.setdefault(root_id, asyncio.Lock())
    async with lock:
        yield


# Per-conversation serialization for the streaming pipeline. The five chat
# streaming routes refuse a second POST against a held lock with an in-band
# SSE error event; /edit, /delete, and /switch-branch share the same lock
# but block on the stream instead of erroring, since they have no SSE
# channel for an "already running" reply and the user expects them to take
# effect rather than fail. Closes: doubled-LLM cost on concurrent /send,
# FK cascade on mid-stream /delete, terminal set_active_leaf clobber of a
# mid-stream /switch-branch, and pre-edit-prefix vs post-edit-DB skew on
# mid-stream /edit. Dict growth shape matches _workflow_root_locks.
_conversation_stream_locks: dict[str, asyncio.Lock] = {}


@asynccontextmanager
async def _conversation_stream_lock(cid: str):
    lock = _conversation_stream_locks.setdefault(cid, asyncio.Lock())
    async with lock:
        yield


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    run_pending(DB_PATH)
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
    editor_audit_toggles: Optional[dict] = None
    hide_streaming_until_baked: Optional[bool] = None
    prevent_prompt_overrides: Optional[bool] = None
    agent_same_as_writer: Optional[bool] = None
    agent_endpoint_id: Optional[int] = None
    agent_shared_system_prompt: Optional[str] = None
    inspector_open_states: Optional[dict] = None


class WorkflowConfigUpdate(BaseModel):
    # Required (no default): a body lacking "config" is a 422, not a silent
    # clear; an explicit {"config": {}} is the intentional reset-to-defaults.
    config: dict


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
    constant: bool = False
    priority: int = 100
    enabled: bool = True


class LorebookEntryUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None
    keywords: Optional[list[str]] = None
    case_insensitive: Optional[bool] = None
    constant: Optional[bool] = None
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
    custom_instructions: Optional[str] = None


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
    enable_agent: bool = True
    attachments: List[AttachmentIn] = []


class RegenerateMsg(BaseModel):
    enable_agent: bool = True


class MagicRewriteMsg(BaseModel):
    direction: str


class PhraseGroupCreate(BaseModel):
    variants: list[str] = []
    kind: str = "literal"
    pattern: str = ""


class PhraseGroupUpdate(BaseModel):
    variants: list[str] = []
    kind: str = "literal"
    pattern: str = ""


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
        raise HTTPException(status_code=400, detail="Mood fragment with this ID already exists")
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
        raise HTTPException(status_code=404, detail="Mood fragment not found or is built-in")
    return {"ok": True}


# Director Fragments ──


@app.get("/api/director-fragments")
async def api_list_director_fragments():
    return await get_director_fragments()


@app.post("/api/director-fragments")
async def api_create_director_fragment(data: DirectorFragmentCreate):
    existing = await get_director_fragment(data.id)
    if existing:
        raise HTTPException(status_code=400, detail="Director fragment with this ID already exists")
    result = await create_director_fragment(data.model_dump())
    if not result:
        raise HTTPException(status_code=500, detail="Failed to create director fragment")
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


async def require_lorebook_entry(entry_id: int, world: dict = Depends(require_world)) -> dict:  # noqa: B008
    entry = await get_lorebook_entry(entry_id)
    if not entry or entry.get("world_id") != world["id"]:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


@app.get("/api/worlds/{world_id}/entries")
async def api_list_lorebook_entries(world: dict = Depends(require_world)):  # noqa: B008
    return await get_lorebook_entries(world["id"])


@app.post("/api/worlds/{world_id}/entries")
async def api_create_lorebook_entry(data: LorebookEntryCreate, world: dict = Depends(require_world)):  # noqa: B008
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
    result = await update_lorebook_entry(entry["id"], data.model_dump(exclude_unset=True))
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
    constant = bool(item.get("constant", False))
    return {
        "name": str(name),
        "content": str(item.get("content") or ""),
        "keywords": keywords,
        "enabled": enabled,
        "priority": priority,
        "case_insensitive": not bool(case_sensitive),
        "constant": constant,
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
        raise HTTPException(status_code=422, detail="entries must be an object or array")

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


def _validate_phrase_group(kind: str, variants: list[str], pattern: str) -> tuple[list[str], str]:
    """Validate a phrase group by kind. Returns (variants, pattern) to persist.

    A group is *either* literal variants *or* a single regex — never both.
    """
    if kind == "regex":
        pattern = (pattern or "").strip()
        if not pattern:
            raise HTTPException(status_code=400, detail="A regex pattern is required")
        try:
            re.compile(pattern)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Invalid regular expression: {e}")
        # Regex groups carry no literal variants.
        return [], pattern

    # Literal group.
    cleaned = [v.strip() for v in (variants or []) if isinstance(v, str) and v.strip()]
    if not cleaned:
        raise HTTPException(status_code=400, detail="At least one variant is required")
    return cleaned, ""


@app.post("/api/phrase-bank")
async def api_create_phrase_group(data: PhraseGroupCreate):
    """Create a new phrase group (literal variants or a single regex)."""
    variants, pattern = _validate_phrase_group(data.kind, data.variants, data.pattern)
    group_id = await add_phrase_group(variants, data.kind, pattern)
    return {"id": group_id, "kind": data.kind, "variants": variants, "pattern": pattern}


@app.put("/api/phrase-bank/{group_id}")
async def api_update_phrase_group(group_id: int, data: PhraseGroupUpdate):
    """Update an existing phrase group (literal variants or a single regex)."""
    variants, pattern = _validate_phrase_group(data.kind, data.variants, data.pattern)
    success = await update_phrase_group(group_id, variants, data.kind, pattern)
    if not success:
        raise HTTPException(status_code=404, detail="Phrase group not found")
    return {"ok": True, "id": group_id, "kind": data.kind, "variants": variants, "pattern": pattern}


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
        post_history_instructions=post_hist,
        character_card_id=card_id,
    )

    # If there's a first message, auto-add it as the first assistant turn
    if first_mes.strip():
        msg_id, _ = await add_message(cid, "assistant", first_mes.strip(), 0, attachments=None)
        await set_active_leaf(cid, msg_id)

        # If we have a character card with alternate greetings, create swipe versions
        if card_id:
            card = await get_character_card(card_id)
            if card:
                alternate_greetings = card.get("alternate_greetings", [])
                count = await insert_alternate_greeting_swipes(cid, alternate_greetings)
                if count:
                    logger.info(f"Created {count} alternate greeting swipes for conversation {cid}")

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
async def api_summarize_conversation(cid: str, data: SummarizeRequest, request: Request):
    """Stream a narrative summary of the conversation history, excluding the last keep_count messages."""
    if data.keep_count not in (2, 4, 6, 8):
        raise HTTPException(status_code=400, detail="keep_count must be one of 2, 4, 6, 8")

    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    messages = await get_messages_with_branch_info(cid)
    history_slice = messages[: max(0, len(messages) - data.keep_count)]

    if not history_slice:
        raise HTTPException(status_code=400, detail="Not enough messages to summarize")

    settings = await get_settings()
    char_name = conv.get("character_name", "Character") or "Character"
    active_persona_id = settings.get("active_persona_id")
    active_persona = await get_user_persona(active_persona_id) if active_persona_id else None
    system_prompt, char_persona, mes_example = await resolve_char_context(conv, settings)
    macros = Macros.from_settings(settings, char_name, active_persona)
    user_description = active_persona.get("description", "") if active_persona else settings.get("user_description", "")

    client = LLMClient(
        settings["endpoint_url"],
        api_key=settings.get("api_key", ""),
        profile=profile_for(settings["endpoint_url"], settings.get("model_name", "")),
    )
    summarizer = ConversationSummarizer(client, settings)
    llm_messages = summarizer.build_messages(
        system_prompt,
        char_persona,
        conv.get("character_scenario", "") or "",
        mes_example,
        ("" if settings.get("prevent_prompt_overrides") else conv.get("post_history_instructions", "")),
        history_slice,
        macros,
        user_description,
        custom_instructions=data.custom_instructions,
    )

    async def _gen():
        try:
            async for delta in summarizer.stream(llm_messages, settings.get("model_name", "")):
                yield {"event": "token", "data": delta}
            yield {"event": "done", "data": ""}
        except Exception as e:
            logger.error("Summarize error: %s", e)
            yield {"event": "error", "data": str(e)}

    return _CleanupStreamingResponse(
        _sse_stream(_gen(), request, client_ref=[client], cid=cid),
        media_type="text/event-stream",
    )


@app.post("/api/conversations/{cid}/compress")
async def api_compress_conversation(cid: str, data: CompressRequest):
    """Create a new conversation seeded with a summary, then re-append the last keep_count messages."""
    if data.keep_count not in (2, 4, 6, 8):
        raise HTTPException(status_code=400, detail="keep_count must be one of 2, 4, 6, 8")
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
        post_history_instructions=conv.get("post_history_instructions", "") or "",
        character_card_id=conv.get("character_card_id"),
    )

    prev_id, _ = await add_message(new_cid, "assistant", data.summary.strip(), 0)
    await set_active_leaf(new_cid, prev_id)

    # Carry user uploads onto the fork; workflow attachments are regenerable and dropped.
    for i, msg in enumerate(tail):
        atts = msg.get("user_attachments") or []
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
        prev_id, _ = await add_message(
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
                        await create_lorebook_entry(world["id"], _normalise_lorebook_entry(item))
            card_data["world_id"] = world["id"]

    try:
        return await create_character_card(card_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@app.post("/api/characters/import")
async def api_import_character(file: Annotated[UploadFile, File(...)]):
    """Import a SillyTavern-compatible character card PNG."""
    if not file.filename or not file.filename.lower().endswith(".png"):
        raise HTTPException(status_code=400, detail="Only .png character card files are supported")

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
        raise HTTPException(status_code=400, detail=f"Failed to parse character card: {e}") from e
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


class ImportUrlRequest(BaseModel):
    source: str
    full_path: str


@app.get("/api/characters/browse")
async def api_browse_characters(source: str = "characterhub", q: str = "", page: int = 1):
    """Proxy external character-card search providers (avoids browser CORS)."""
    return await card_downloader.browse(source, q, page)


@app.get("/api/characters/randomize")
async def api_randomize_characters(source: str = "characterhub", q: str = ""):
    """Return a randomized selection from a source that supports randomize."""
    return await card_downloader.randomize(source, q)


@app.post("/api/characters/import-url")
async def api_import_character_url(req: ImportUrlRequest):
    """Download a character card from an external source and run it through the
    same parse pipeline as /api/characters/import."""
    return await card_downloader.download_card(req.source, req.full_path)


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
            logger.warning("Avatar data for card %s is corrupt; exporting without avatar", card_id)
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
                    "constant": bool(e.get("constant", False)),
                    "name": e["name"],
                    "priority": e["priority"],
                    "id": e["id"],
                }
                for e in entries
            ],
        }

    png_bytes = tavern_cards.to_png(card, avatar_bytes)

    safe_name = "".join(c for c in card.get("name", "character") if c.isalnum() or c in " _-").strip() or "character"
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
    gen,
    request: Request,
    *,
    client_ref: list | None = None,
    cid: str | None = None,
):
    """Wrap an event-dict async generator as SSE, stopping cleanly on client disconnect.

    The primary stop path is the explicit POST /stop endpoint, which calls
    LLMClient.abort() directly. That in turn breaks out of the asyncio.wait()
    loop in complete() and lets the async-with block close the TCP connection
    to the LLM server normally — no task cancellation needed.

    A background watcher also polls request.is_disconnected() as a fallback
    for cases like the user closing the browser tab without clicking Stop.
    """

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

    lock: asyncio.Lock | None = None
    watcher: asyncio.Task | None = None
    try:
        if cid is not None:
            # locked()/acquire() are atomic across coroutines (no await between)
            # so a held-lock loser deterministically takes the error branch and
            # does not queue, while an open-lock winner acquires without ever
            # suspending. Lock is set only after acquire() returns so the
            # finally's release guard skips both the error branch and any
            # acquire-cancelled path.
            candidate = _conversation_stream_locks.setdefault(cid, asyncio.Lock())
            if candidate.locked():
                yield "event: error\ndata: Another generation is already running\n\n"
                return
            await candidate.acquire()
            lock = candidate
        watcher = asyncio.create_task(_watch_disconnect())
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
        if watcher is not None:
            watcher.cancel()
        if cid and lock is not None:
            # `lock is not None` implies this coroutine won the acquire race and
            # therefore owns whatever was lazy-registered into _active_clients.
            # Gating the pop on the same sentinel keeps the rejected-loser path
            # from deleting the winner's entry and silently no-opping /stop.
            _active_clients.pop(cid, None)
        if lock is not None:
            # Release before gen.aclose() so a queued /edit, /delete, or
            # /switch-branch can proceed in parallel with the inner generator's
            # cleanup rather than waiting on it.
            lock.release()
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
async def api_edit_message(cid: str, msg_id: int, data: EditMessage):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Serialize against an in-flight streaming pipeline on this cid: the
    # pipeline reads message content into the LLM prefix early and persists
    # the assistant reply late, so a mid-stream edit would make the on-disk
    # user message disagree with the prefix that produced the reply.
    async with _conversation_stream_lock(cid):
        original = await get_message_by_id(msg_id)
        if not original or original["conversation_id"] != cid:
            raise HTTPException(status_code=404, detail="Message not found")

        await update_message_content(msg_id, data.content)
        return {"ok": True}


@app.delete("/api/conversations/{cid}/messages/{msg_id}")
async def api_delete_message(cid: str, msg_id: int):
    """Delete a message and all its descendants. Returns updated message list."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Serialize against an in-flight streaming pipeline on this cid: ON
    # DELETE CASCADE on messages.parent_id would otherwise wipe the
    # in-flight assistant row mid-INSERT (IntegrityError) or right after
    # commit (silent disappearance).
    async with _conversation_stream_lock(cid):
        if not await delete_message_with_descendants(cid, msg_id):
            raise HTTPException(status_code=404, detail="Message not found")
        return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/switch-branch")
async def api_switch_branch(cid: str, msg_id: int):
    """Switch to the branch containing msg_id (sets active leaf to deepest descendant)."""
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    # Serialize against an in-flight streaming pipeline on this cid: the
    # pipeline's terminal set_active_leaf would otherwise overwrite the
    # branch the user just selected.
    async with _conversation_stream_lock(cid):
        success = await switch_to_branch(cid, msg_id)
        if not success:
            raise HTTPException(status_code=404, detail="Message not found")
        return await get_messages_with_branch_info(cid)


@app.post("/api/conversations/{cid}/messages/{msg_id}/regenerate")
async def api_regenerate_msg(cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None):
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
async def api_super_regenerate_msg(cid: str, msg_id: int, request: Request, data: Optional[RegenerateMsg] = None):
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
async def api_magic_rewrite_msg(cid: str, msg_id: int, request: Request, data: MagicRewriteMsg):
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


@app.get("/api/conversations/{cid}/messages/{msg_id}/director-log")
async def api_get_message_director_log(cid: str, msg_id: int):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg = await get_message_by_id(msg_id)
    if not msg or msg.get("conversation_id") != cid:
        raise HTTPException(status_code=404, detail="Message not found")
    log = await get_director_log_for_message(msg_id)
    if not log:
        return {
            "active_moods": [],
            "tool_calls": [],
            "injection_block": "",
            "agent_latency_ms": 0,
            "reasoning_director": "",
            "reasoning_writer": "",
            "reasoning_editor": "",
        }
    return {
        "active_moods": log.get("active_moods_after", []),
        "tool_calls": log.get("tool_calls", []),
        "injection_block": log.get("injection_block", ""),
        "agent_latency_ms": log.get("agent_latency_ms", 0),
        "reasoning_director": log.get("reasoning_director") or "",
        "reasoning_writer": log.get("reasoning_writer") or "",
        "reasoning_editor": log.get("reasoning_editor") or "",
    }


@app.get("/api/conversations/{cid}/context-size")
async def api_get_context_size(cid: str):
    conv = await get_conversation(cid)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    settings = await get_settings()
    messages = await get_messages(cid)
    director = await get_director_state(cid) or {}
    director_frags = [f for f in await get_director_fragments() if f.get("enabled", True)]
    mood_frags = [f for f in await get_mood_fragments() if f.get("enabled", True)]
    lorebook_entries = await get_active_lorebook_entries()

    # Resolve persona
    persona_id = settings.get("active_persona_id")
    active_persona = await get_user_persona(persona_id) if persona_id else None
    macros = Macros.from_settings(settings, conv["character_name"], active_persona)
    user_desc = active_persona.get("description", "") if active_persona else settings.get("user_description", "")

    # Resolve character context
    system_prompt, char_persona, mes_example = await resolve_char_context(conv, settings)

    # Measure each component individually
    sys_text = system_prompt or ""
    persona_text = macros.resolve_message(char_persona or "")
    scenario_text = macros.resolve_message(conv.get("character_scenario", "") or "")
    mes_text = macros.resolve_message(mes_example or "")
    post_text = macros.resolve_message(
        "" if settings.get("prevent_prompt_overrides") else (conv.get("post_history_instructions", "") or "")
    )
    user_persona_text = f"## User: {macros.user}\n{macros.resolve_message(user_desc)}" if user_desc else ""
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
    scan_depth = prompt_builder.LOREBOOK_SCAN_DEPTH
    recent_messages = messages[-scan_depth:] if len(messages) >= scan_depth else messages
    lorebook_block = prompt_builder.compute_lorebook_injection_block(recent_messages, lorebook_entries, macros)

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
            handle_turn(cid, data.content, attachments=attachments, client_ref=client_ref),
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
        raise HTTPException(status_code=404, detail="Conversation not found")
    messages = await get_messages(cid)
    if not messages or messages[-1]["role"] != "user":
        raise HTTPException(status_code=400, detail="Last message is not a user message")
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


# Workflows --


@app.get("/api/workflows")
async def api_list_workflows():
    """Manifest the frontend reads once at boot to populate Secondary tabs and buttons."""
    return [
        {
            "id": w.id,
            "display_name": w.display_name,
            "config_schema": w.config_schema,
            "config_defaults": w.config_defaults,
        }
        for w in list_workflows()
    ]


@app.put("/api/workflows/{workflow_id}/config")
async def api_set_workflow_config(workflow_id: str, data: WorkflowConfigUpdate):
    """Persist a workflow's global config slot as a full replacement."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    # Serialize the replacement with workflow code that updates the same slot via
    # a locked read-modify-write; a lock-free write here could be lost mid-RMW.
    async with workflow_config_lock():
        await set_workflow_config(workflow_id, data.config)
        effective = await get_workflow_config(workflow_id)
    logger.info("workflow %r config updated (%d keys)", workflow_id, len(data.config))
    return {"config": effective}


@app.get("/api/workflows/{workflow_id}/config")
async def api_get_workflow_config(workflow_id: str):
    """Return a workflow's effective config: persisted slot, else its defaults."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    return {"config": await get_workflow_config(workflow_id)}


@app.post("/api/conversations/{cid}/workflows/{workflow_id}/trigger")
async def api_trigger_workflow(cid: str, workflow_id: str, body: dict = Body(default={})):  # noqa: B008
    """Run a workflow's on_demand hook against the current conversation state."""
    sub = get_subscription(workflow_id, HookType.ON_DEMAND)
    if sub is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    # Serialize against the pre/post hook iteration of an in-flight pipeline and
    # against any other /trigger for the same (cid, workflow_id), so the prior
    # workflow_state read the hook depends on cannot be clobbered between read
    # and write by a concurrent caller.
    async with workflow_state_lock(cid, workflow_id):
        conv = await get_conversation(cid)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        card_id = conv.get("character_card_id")
        card = await get_character_card(card_id) if card_id else None
        msgs = await get_messages(cid)
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
            profile=profile_for(settings_snapshot["endpoint_url"], settings_snapshot.get("model_name", "")),
        )
        async with workflow_character_state_lock(conv.get("character_card_id") or "", workflow_id):
            try:
                od_ctx = OnDemandCtx(
                    conversation_id=cid,
                    history=_readonly(msgs),
                    last_user_message=last_user,
                    settings=_readonly(settings_snapshot),
                    client=client,
                    character_id=conv.get("character_card_id"),
                    character=_readonly(card),
                )
                return await sub.callable(od_ctx, body)
            except Exception:
                logger.exception("on_demand hook %r failed", workflow_id)
                raise HTTPException(status_code=500, detail="On-demand handler raised; see server logs")


@app.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate")
async def api_regenerate_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008
    """Append a new sibling variant under a workflow-produced attachment's root."""
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    wid = att.get("workflow_id")
    sub = get_subscription(wid, HookType.REGENERATE) if wid else None
    if sub is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow {wid!r} is not registered or has no regenerate handler",
        )
    # Single hop suffices: the dispatcher itself assigns parent_attachment_id = root_id
    # on every write, so the variant tree is flat by construction (root + N siblings).
    root_id = att["parent_attachment_id"] or aid

    async with _workflow_root_lock(root_id):
        anchor = await get_message_by_id(mid)
        if anchor is None or anchor["conversation_id"] != cid:
            raise HTTPException(status_code=404, detail="Message not found in conversation")
        msgs = await get_messages_before(cid, mid)
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
            profile=profile_for(settings_snapshot["endpoint_url"], settings_snapshot.get("model_name", "")),
        )

        card_id = conv.get("character_card_id")
        card = await get_character_card(card_id) if card_id else None
        try:
            regen_ctx = RegenCtx(
                conversation_id=cid,
                message_id=mid,
                attachment_id=aid,
                original_attachment=_readonly(att),
                history=_readonly(msgs),
                last_user_message=last_user,
                settings=_readonly(settings_snapshot),
                client=client,
                character_id=conv.get("character_card_id"),
                character=_readonly(card),
            )
            new_dicts = await sub.callable(regen_ctx, body)
        except Exception:
            logger.exception("regenerate hook %r failed for attachment %r", wid, aid)
            raise HTTPException(status_code=500, detail="Regenerate handler raised; see server logs")

        if not isinstance(new_dicts, list):
            logger.warning(
                "regenerate hook %r returned non-list (%s); treating as empty",
                wid,
                type(new_dicts).__name__,
            )
            new_dicts = []

        # Bad-shape entries are partitioned to rejected_workflow_atts so a
        # single bad entry does not roll back the batch insert. Non-dict
        # entries are dropped instead of rejected because the rejection
        # record requires a filename to surface in the UI.
        fixed: list[dict] = []
        rejected_pre: list[dict] = []
        for d in new_dicts:
            if not isinstance(d, dict):
                logger.warning("regenerate hook %r returned non-dict entry; skipping", wid)
                continue
            candidate = {**d, "workflow_id": sub.workflow_id, "parent_attachment_id": root_id}
            ok, reason = validate_workflow_attachment_shape(candidate)
            if not ok:
                rejected_pre.append(
                    {
                        "filename": candidate.get("filename") if isinstance(candidate.get("filename"), str) else None,
                        "workflow_id": sub.workflow_id,
                        "mime": candidate.get("mime") if isinstance(candidate.get("mime"), str) else None,
                        "reason": reason,
                        "originating_attachment_id": root_id,
                    }
                )
                logger.info(
                    "regenerate hook %r returned attachment rejected by shape validator: %s",
                    wid,
                    reason,
                )
                continue
            fixed.append(candidate)

        if not fixed and not rejected_pre:
            return {"attachments": [], "rejected_workflow_atts": []}

        try:
            new_ids, helper_rejected = await insert_workflow_attachments(mid, fixed)
        except (ValueError, LookupError, OSError):
            logger.exception("regenerate hook %r batch insert failed", wid)
            raise HTTPException(status_code=500, detail="Regenerate batch insert failed; see server logs")

        helper_rejected_projected = [
            {
                "filename": a.get("filename"),
                "workflow_id": a.get("workflow_id"),
                "mime": a.get("mime"),
                "reason": a.get("reason") or OVERSIZE_NO_METADATA_REASON,
                "originating_attachment_id": root_id,
            }
            for a in helper_rejected
        ]
        return {
            "attachments": new_ids,
            "rejected_workflow_atts": rejected_pre + helper_rejected_projected,
        }


def _decode_stored_consumption_metadata(att: dict) -> dict | None:
    """Parse the parent attachment's stored consumption_metadata JSON.

    Returns the decoded dict, or ``None`` for any malformed or non-dict value.
    """
    raw = att.get("consumption_metadata")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _split_reroll_gen_result(result, workflow_id: str | None) -> tuple[object, dict | None]:
    """Split a reroll_gen hook return into ``(data, consumption_metadata)``.

    A raw ``bytes`` return carries no metadata; a ``(bytes, dict | None)``
    tuple supplies a fresh ``consumption_metadata``. A non-dict second element
    is dropped with a warning. The caller validates that ``data`` is non-empty
    bytes. Shared by the reroll-gen and rehydrate routes so both interpret the
    hook return identically.
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (bytes, bytearray)):
        data, consumption_metadata = result
        if consumption_metadata is not None and not isinstance(consumption_metadata, dict):
            logger.warning(
                "reroll_gen hook %r returned tuple with non-dict consumption_metadata (%s); coercing to None",
                workflow_id,
                type(consumption_metadata).__name__,
            )
            consumption_metadata = None
        return data, consumption_metadata
    return result, None


def _build_reroll_gen_ctx(cid: str, mid: int, aid: int, att: dict, settings: dict, client) -> RerollGenCtx:
    prior_cm = _decode_stored_consumption_metadata(att)
    return RerollGenCtx(
        conversation_id=cid,
        message_id=mid,
        attachment_id=aid,
        original_attachment=_readonly(att),
        settings=_readonly(settings),
        client=client,
        prior_consumption_metadata=_readonly(prior_cm) if prior_cm is not None else None,
    )


def _generated_seed() -> str:
    import secrets

    return secrets.token_hex(16)


@app.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen")
async def api_reroll_gen_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008, ARG001
    """Generate a new sibling using the original's stored generation_metadata
    with a freshly minted seed.

    The new sibling persists the new seed alongside the inherited
    generation_metadata so it is itself rehydratable; without that, an
    evict-then-rehydrate cycle would lose the rerolled output.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    wid = att.get("workflow_id")
    sub = get_subscription(wid, HookType.REROLL_GEN) if wid else None
    if sub is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow {wid!r} is not registered or has no reroll_gen handler",
        )

    metadata_raw = att.get("generation_metadata")
    try:
        params = json.loads(metadata_raw) if metadata_raw else {}
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}

    root_id = att["parent_attachment_id"] or aid

    async with _workflow_root_lock(root_id):
        seed = _generated_seed()
        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
            profile=profile_for(settings_snapshot["endpoint_url"], settings_snapshot.get("model_name", "")),
        )

        try:
            ctx = _build_reroll_gen_ctx(cid, mid, aid, att, settings_snapshot, client)
            result = await sub.callable(ctx, params, seed)
        except Exception:
            logger.exception("reroll_gen hook %r failed for attachment %r", wid, aid)
            raise HTTPException(status_code=500, detail="reroll_gen handler raised; see server logs")

        data, new_consumption_metadata = _split_reroll_gen_result(result, wid)

        if not isinstance(data, (bytes, bytearray)) or not data:
            raise HTTPException(status_code=500, detail="reroll_gen handler returned no bytes")

        new_attachment = {
            "workflow_id": sub.workflow_id,
            "parent_attachment_id": root_id,
            "filename": att.get("filename") or sub.workflow_id,
            "mime": att.get("mime_type") or "application/octet-stream",
            "data": bytes(data),
            "seed": seed,
            "generation_metadata": params,
            "consumption_metadata": new_consumption_metadata,
            "annotation": att.get("annotation"),
        }
        try:
            new_id, rejected = await insert_workflow_attachment(mid, new_attachment)
        except (ValueError, LookupError, OSError):
            logger.exception("reroll_gen hook %r yielded an attachment that failed insert", wid)
            raise HTTPException(status_code=500, detail="reroll_gen insert failed; see server logs")

        return {
            "attachment_id": new_id,
            "rejected_workflow_atts": (
                [
                    {
                        "filename": rejected.get("filename"),
                        "workflow_id": rejected.get("workflow_id"),
                        "mime": rejected.get("mime"),
                        "reason": rejected.get("reason") or OVERSIZE_NO_METADATA_REASON,
                        "originating_attachment_id": root_id,
                    }
                ]
                if rejected is not None
                else []
            ),
        }


@app.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate")
async def api_rehydrate_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008, ARG001
    """Recover bytes for an evicted attachment using its stored seed + params.

    Preconditions:
      - The row's `data_b64` is the EVICTED_MARKER sentinel.
      - The row has a non-NULL `seed`.

    The framework calls the workflow's `reroll_gen` hook with the stored
    params and stored seed, then writes the returned bytes back into the
    same row's data_b64. No new sibling is created.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    if att.get("data_b64") != EVICTED_MARKER:
        raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate")
    seed = att.get("seed")
    if not seed:
        raise HTTPException(status_code=409, detail="Attachment has no stored seed; cannot rehydrate")

    # Serialize same-root rehydrates the way /regenerate, /reroll-gen, and
    # /activate already do for their sibling-tree mutations. Without this,
    # two concurrent callers would each run the full reroll_gen LLM call
    # before the cache helper's transactional recheck deduplicates them at
    # the DB layer -- doubling LLM cost even though the row stays consistent.
    # parent_attachment_id is NULL on root rows, so `or aid` resolves to the
    # root id whether the request targets a sibling or the root itself.
    root_id = att["parent_attachment_id"] or aid
    async with _workflow_root_lock(root_id):
        # Re-read inside the lock so a concurrent caller that already
        # rehydrated cannot slip past the snapshot check above and double
        # the reroll_gen LLM call before the cache helper's transactional
        # recheck deduplicates the bytes write.
        att = await get_workflow_attachment_by_id(aid)
        if att is None or att.get("data_b64") != EVICTED_MARKER:
            raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate")
        wid = att.get("workflow_id")
        sub = get_subscription(wid, HookType.REROLL_GEN) if wid else None
        if sub is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {wid!r} is not registered or has no reroll_gen handler",
            )

        metadata_raw = att.get("generation_metadata")
        try:
            params = json.loads(metadata_raw) if metadata_raw else {}
        except (TypeError, ValueError):
            params = {}
        if not isinstance(params, dict):
            params = {}

        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
            profile=profile_for(settings_snapshot["endpoint_url"], settings_snapshot.get("model_name", "")),
        )

        try:
            ctx = _build_reroll_gen_ctx(cid, mid, aid, att, settings_snapshot, client)
            result = await sub.callable(ctx, params, seed)
        except Exception:
            logger.exception("reroll_gen (rehydrate) %r failed for attachment %r", wid, aid)
            raise HTTPException(status_code=500, detail="reroll_gen handler raised; see server logs")

        data, new_consumption_metadata = _split_reroll_gen_result(result, wid)

        if not isinstance(data, (bytes, bytearray)) or not data:
            raise HTTPException(status_code=500, detail="reroll_gen handler returned no bytes")

        try:
            await rehydrate_attachment(aid, bytes(data), consumption_metadata=new_consumption_metadata)
        except RehydrateAlreadyDoneError:
            # Race with a concurrent rehydrate that already restored the bytes.
            # End state is correct; surface as 409 so the client treats it as
            # success rather than the generic 500.
            raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate")
        except (LookupError, ValueError):
            logger.exception("rehydrate write failed for attachment %r", aid)
            raise HTTPException(status_code=500, detail="rehydrate write failed; see server logs")

        return {"attachment_id": aid}


@app.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/activate")
async def api_activate_workflow_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008
    """Persist the user's active-sibling choice for a workflow attachment group.

    ``aid`` is the ROOT attachment id (``parent_attachment_id IS NULL``).
    Body shape: ``{"sibling_id": int | null}`` -- ``null`` clears the
    column, which reverts to "newest sibling wins" in the renderer.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")

    raw_sibling_id = body.get("sibling_id") if isinstance(body, dict) else None
    if raw_sibling_id is not None and (not isinstance(raw_sibling_id, int) or isinstance(raw_sibling_id, bool)):
        raise HTTPException(status_code=400, detail="sibling_id must be an integer or null")

    try:
        async with _workflow_root_lock(aid):
            await set_active_sibling(aid, raw_sibling_id, expected_message_id=mid)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"active_sibling_id": raw_sibling_id}


@app.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete")
async def api_delete_workflow_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008
    """Delete a workflow attachment: one variant, or the whole group.

    ``aid`` is the acted-on row. Body: ``{"scope": "variant" | "group"}``.
    Deleting the root variant of a multi-variant group promotes the oldest
    survivor to root; the response ``root_id`` reports the resulting root.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    scope = body.get("scope") if isinstance(body, dict) else None
    if scope not in ("variant", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'variant' or 'group'")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    root_id = att["parent_attachment_id"] or aid
    try:
        async with _workflow_root_lock(root_id):
            result = await delete_workflow_attachments(aid, scope=scope, expected_message_id=mid)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return result


@app.post("/api/conversations/{cid}/workflow-attachments/access")
async def api_record_workflow_attachment_access(cid: str, body: dict = Body(default={})):  # noqa: B008
    """Record access events for workflow attachments.

    Body shape: ``{"ids": [int, ...]}``. Counter values are assigned in
    input-list order, so callers can encode intra-call ordering.

    Ids not belonging to this conversation are silently dropped rather
    than raising: the frontend can legitimately hold stale ids around a
    swipe / regen race, and a 400 there would be a user-visible failure
    on an ignorable client/server skew.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    raw_ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list of integers")

    int_ids: list[int] = []
    for v in raw_ids:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            int_ids.append(v)

    if not int_ids:
        return {"ok": True, "recorded": 0}

    placeholders = ",".join("?" * len(int_ids))
    async with get_db() as db_conn:
        rows = list(
            await db_conn.execute_fetchall(
                f"SELECT wa.id FROM workflow_attachments wa "  # nosec B608 -- placeholders only
                f"JOIN messages m ON m.id = wa.message_id "
                f"WHERE m.conversation_id = ? AND wa.id IN ({placeholders})",
                (cid, *int_ids),
            )
        )
    valid_ids_set = {r["id"] for r in rows}
    ordered_valid = [i for i in int_ids if i in valid_ids_set]

    await record_access(ordered_valid)
    return {"ok": True, "recorded": len(ordered_valid)}


# Frontend serving ──


@app.get("/")
async def serve_frontend():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# Mount static files last
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")
