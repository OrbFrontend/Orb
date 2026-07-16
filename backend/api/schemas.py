"""Pydantic request/response models for the HTTP API.

These are the wire-shape contracts for the route modules under
``api/routes/``. Kept in one module so a route file imports only the models
it needs and the shapes stay discoverable in one place.
"""

from __future__ import annotations

from typing import Any, List, Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator


class SettingsUpdate(BaseModel):
    model_config = {"protected_namespaces": ()}

    endpoint_url: Optional[str] = None
    api_key: Optional[str] = None
    model_name: Optional[str] = None
    # Hyperparameters (temperature, min_p, top_k, top_p, repetition_penalty,
    # max_tokens) are intentionally NOT on this contract: they live on the active
    # endpoint's model_config and are edited via /models/{id}. get_settings()
    # overlays them for reads, so a write here would be silently discarded. The
    # frontend still includes them in its /settings PUT payload; extra fields are
    # ignored (default Pydantic behavior), mirroring completion_mode.
    shared_system_prompt: Optional[str] = None
    system_prompt: Optional[str] = None
    user_name: Optional[str] = None
    user_description: Optional[str] = None
    enabled_tools: Optional[dict[str, bool]] = None
    enable_agent: Optional[bool] = None
    length_guard_enabled: Optional[bool] = None
    length_guard_enforce: Optional[bool] = None
    agentic_lorebook_enabled: Optional[bool] = None
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
    feedback_enabled: Optional[bool] = None
    director_individual_fragments: Optional[bool] = None
    direction_notes_record: Optional[bool] = None
    direction_notes_inject: Optional[Literal["off", "director", "writer", "both"]] = None
    inspector_open_states: Optional[dict] = None
    workflows_globally_enabled: Optional[bool] = None
    retry_enabled: Optional[bool] = None
    retry_count: Optional[int] = None
    retry_delay_seconds: Optional[float] = None


class DirectionNoteUpdate(BaseModel):
    content: str


class DirectionNoteCreate(BaseModel):
    # message_id anchors the note to a turn (its turn_index is derived at read time);
    # the route rejects an id that is not an assistant message in this conversation.
    message_id: int
    label: str
    content: str


class WorkflowConfigUpdate(BaseModel):
    # Required (no default): a body lacking "config" is a 422, not a silent
    # clear; an explicit {"config": {}} is the intentional reset-to-defaults.
    config: dict


class WorkflowEnabledUpdate(BaseModel):
    # Required (no default): a body lacking "enabled" is a 422, mirroring
    # WorkflowConfigUpdate -- the per-workflow toggle is never an implicit value.
    enabled: bool


class EndpointCreate(BaseModel):
    url: str
    api_key: str = ""


class EndpointUpdate(BaseModel):
    url: Optional[str] = None
    api_key: Optional[str] = None
    active_model_config_id: Optional[int] = None
    agent_active_model_config_id: Optional[int] = None
    completion_mode: Optional[Literal["chat", "text"]] = None
    proxy: Optional[str] = None

    @field_validator("proxy")
    @classmethod
    def _validate_proxy(cls, v: Optional[str]) -> Optional[str]:
        # Empty/blank means "no proxy". A set value must use a scheme httpx
        # accepts (http/https, or socks5 via the httpx[socks] extra); reject
        # anything else here so a typo fails at save time, not on every LLM turn.
        if v is None:
            return v
        v = v.strip()
        if not v:
            return ""
        if urlsplit(v).scheme.lower() not in ("http", "https", "socks5"):
            raise ValueError("proxy URL must start with http://, https://, or socks5://")
        return v


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
    reasoning_effort: str = ""
    reasoning_effort_param: str = ""
    reasoning_effort_value: str = ""


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
    reasoning_effort: Optional[str] = None
    reasoning_effort_param: Optional[str] = None
    reasoning_effort_value: Optional[str] = None


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


class InteractiveFragmentCreate(BaseModel):
    id: str
    label: str
    description: str
    field_type: str = "string"
    required: bool = False
    enabled: bool = True
    injection_label: str
    sort_order: int = 0
    direction_note_timing: Literal["pre_writer", "post_turn"] = "post_turn"


class InteractiveFragmentUpdate(BaseModel):
    label: Optional[str] = None
    description: Optional[str] = None
    field_type: Optional[str] = None
    required: Optional[bool] = None
    enabled: Optional[bool] = None
    injection_label: Optional[str] = None
    sort_order: Optional[int] = None
    direction_note_timing: Optional[Literal["pre_writer", "post_turn"]] = None


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
    # Persona lock for this conversation; an explicit null clears it (the route
    # uses model_dump(exclude_unset=True), so absence leaves it untouched).
    persona_lock_id: Optional[int] = None


class SummarizeRequest(BaseModel):
    keep_count: int  # must be one of 2, 4, 6, 8
    custom_instructions: Optional[str] = None


class CompressRequest(BaseModel):
    summary: str
    keep_count: int  # must be one of 2, 4, 6, 8


class CheckpointRequest(BaseModel):
    title: Optional[str] = None


class DocumentSpan(BaseModel):
    # Offsets are JS/UTF-16-domain and opaque to the backend — only shape-validated.
    # ge=0 only, deliberately NO coupling to len(content): Python counts code points
    # and JS counts UTF-16 units, so a valid JS offset can legitimately exceed
    # Python's string length on emoji-bearing docs (see plan design table).
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class DocumentCreate(BaseModel):
    title: Optional[str] = None


class DocumentUpdate(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    generated_spans: Optional[List[DocumentSpan]] = None

    @model_validator(mode="after")
    def _spans_need_content(self) -> "DocumentUpdate":
        # content and generated_spans must travel together: spans without content
        # would apply offsets to stale server-side text. Title-only updates are
        # unaffected (neither field set). Uses model_fields_set so an explicit
        # content="" still counts as "provided".
        if "generated_spans" in self.model_fields_set and "content" not in self.model_fields_set:
            raise ValueError("generated_spans requires content in the same update")
        return self


class DocumentGenerateRequest(BaseModel):
    prompt: str
    # Assisted continuation: interpret ### SYSTEM/USER/ASSISTANT line macros and
    # render through the model's chat template. Defaults false → Raw (verbatim).
    assisted: bool = False
    # Capture per-token alternatives (mikupad-style token swapping). Off by
    # default: logprobs cost generation speed on llama.cpp, and providers that
    # can't supply them degrade to no-popup. Emits `event: probs` SSE frames.
    token_probs: bool = False


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
    # Persona lock for this character card; an explicit null clears it (handled
    # via model_fields_set in api_update_character since the route drops Nones).
    persona_lock_id: Optional[int] = None


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
            raise ValueError("Invalid base64 string") from None
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


class AutocompleteInput(BaseModel):
    draft: str


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


class ResetConfirm(BaseModel):
    confirm: bool


class ImportUrlRequest(BaseModel):
    source: str
    full_path: str


class PresetExportRequest(BaseModel):
    domains: List[str]
    strip_keys: bool = True
    label: str = ""
