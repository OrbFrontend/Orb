"""Pydantic request/response models for the HTTP API.

These are the wire-shape contracts for the route modules under
``api/routes/``. Kept in one module so a route file imports only the models
it needs and the shapes stay discoverable in one place.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from ..core.domain_types import AgentLane, CompletionMode


class SettingsUpdate(BaseModel):
    model_config = {"protected_namespaces": ()}

    endpoint_url: str | None = None
    api_key: str | None = None
    model_name: str | None = None
    # Hyperparameters (temperature, min_p, top_k, top_p, repetition_penalty,
    # max_tokens) are intentionally NOT on this contract: they live on the active
    # endpoint's model_config and are edited via /models/{id}. get_settings()
    # overlays them for reads, so a write here would be silently discarded. The
    # frontend still includes them in its /settings PUT payload; extra fields are
    # ignored (default Pydantic behavior), mirroring completion_mode.
    shared_system_prompt: str | None = None
    system_prompt: str | None = None
    user_name: str | None = None
    user_description: str | None = None
    enabled_tools: dict[str, bool] | None = None
    enable_agent: bool | None = None
    length_guard_enabled: bool | None = None
    length_guard_enforce: bool | None = None
    agentic_lorebook_enabled: bool | None = None
    length_guard_max_words: int | None = None
    length_guard_max_paragraphs: int | None = None
    reasoning_enabled_passes: dict | None = None
    reasoning_prefill_passes: dict | None = None
    active_persona_id: int | None = None
    character_library_view: str | None = None
    character_library_sort: str | None = None
    active_endpoint_id: int | None = None
    show_editor_diff: bool | None = None
    editor_audit_toggles: dict | None = None
    # Document-mode Output Auditor (doc-owned columns; deliberately not shared
    # with editor_audit_toggles so a doc-mode save can't perturb chat scanners).
    document_audit_enabled: bool | None = None
    document_audit_autopatch: bool | None = None
    document_audit_toggles: dict | None = None
    hide_streaming_until_baked: bool | None = None
    prevent_prompt_overrides: bool | None = None
    agent_same_as_writer: bool | None = None
    agent_endpoint_id: int | None = None
    agent_shared_system_prompt: str | None = None
    feedback_enabled: bool | None = None
    director_individual_fragments: bool | None = None
    direction_notes_record: bool | None = None
    direction_notes_inject: Literal["off", "director", "writer", "both"] | None = None
    inspector_open_states: dict | None = None
    workflows_globally_enabled: bool | None = None
    # Floor, not a formality: the cap is enforced by evicting on the next
    # attachment write, so a fumbled 0 would blank the whole artifact cache.
    attachment_cache_budget_bytes: int | None = Field(default=None, ge=50 * 1024 * 1024)


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
    url: str | None = None
    api_key: str | None = None
    active_model_config_id: int | None = None
    agent_active_model_config_id: int | None = None
    completion_mode: CompletionMode | None = None
    proxy: str | None = None

    @field_validator("proxy")
    @classmethod
    def _validate_proxy(cls, v: str | None) -> str | None:
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
    role: AgentLane = "writer"
    reasoning_effort: str = ""
    reasoning_effort_param: str = ""
    reasoning_effort_value: str = ""


class ModelConfigUpdate(BaseModel):
    model_config = {"protected_namespaces": ()}

    model_name: str | None = None
    system_prompt: str | None = None
    temperature: float | None = None
    min_p: float | None = None
    top_k: int | None = None
    top_p: float | None = None
    repetition_penalty: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    reasoning_effort_param: str | None = None
    reasoning_effort_value: str | None = None


class MoodFragmentCreate(BaseModel):
    id: str
    label: str
    description: str
    prompt_text: str
    negative_prompt: str = ""
    enabled: bool = True


class MoodFragmentUpdate(BaseModel):
    label: str | None = None
    description: str | None = None
    prompt_text: str | None = None
    negative_prompt: str | None = None
    enabled: bool | None = None


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
    # Per-instance configuration for an extension-defined field_type. Left as a
    # bare dict here: its real shape is the installed type's own declared
    # instance schema, which this layer cannot know and must not half-check.
    type_config: dict = {}


class InteractiveFragmentUpdate(BaseModel):
    label: str | None = None
    description: str | None = None
    field_type: str | None = None
    required: bool | None = None
    enabled: bool | None = None
    injection_label: str | None = None
    sort_order: int | None = None
    direction_note_timing: Literal["pre_writer", "post_turn"] | None = None
    type_config: dict | None = None


class WorldCreate(BaseModel):
    name: str


class WorldUpdate(BaseModel):
    name: str | None = None
    enabled: bool | None = None


class LorebookEntryCreate(BaseModel):
    name: str
    content: str = ""
    keywords: list[str] = []
    case_insensitive: bool = True
    constant: bool = False
    use_regex: bool = False
    selective: bool = False
    secondary_keys: list[str] = []
    priority: int = 100
    enabled: bool = True


class LorebookEntryUpdate(BaseModel):
    name: str | None = None
    content: str | None = None
    keywords: list[str] | None = None
    case_insensitive: bool | None = None
    constant: bool | None = None
    use_regex: bool | None = None
    selective: bool | None = None
    secondary_keys: list[str] | None = None
    priority: int | None = None
    enabled: bool | None = None


class LorebookImportPayload(BaseModel):
    # Accepts raw lorebook JSON as parsed by the frontend.
    # Supports three common formats:
    #   - SillyTavern standalone lorebook: {"entries": {"0": {...}, "1": {...}}}
    #     where each entry has `key` (list), `comment`, `content`, `disable`, `order`, `caseSensitive`,
    #     plus `selective` / `keysecondary`
    #   - Tavern V2 character_book: {"entries": [...]}
    #     where each entry has `keys`, `name`, `content`, `enabled`, `insertion_order`, `case_sensitive`
    #   - Character Card V3 character_book: as V2, plus `use_regex` and `selective`/`secondary_keys`
    entries: Any


class ConversationCreate(BaseModel):
    title: str = "New Conversation"
    character_card_id: str | None = None
    character_name: str = ""
    character_scenario: str = ""
    first_mes: str = ""
    post_history_instructions: str = ""


class ConversationUpdate(BaseModel):
    title: str | None = None
    # Persona lock for this conversation; an explicit null clears it (the route
    # uses model_dump(exclude_unset=True), so absence leaves it untouched).
    persona_lock_id: int | None = None


class SummarizeRequest(BaseModel):
    keep_count: int  # must be one of 2, 4, 6, 8
    custom_instructions: str | None = None


class CompressRequest(BaseModel):
    summary: str
    keep_count: int  # must be one of 2, 4, 6, 8


class CheckpointRequest(BaseModel):
    title: str | None = None


class DocumentSpan(BaseModel):
    # Offsets are JS/UTF-16-domain and opaque to the backend — only shape-validated.
    # ge=0 only, deliberately NO coupling to len(content): Python counts code points
    # and JS counts UTF-16 units, so a valid JS offset can legitimately exceed
    # Python's string length on emoji-bearing docs (see plan design table).
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class DocumentCreate(BaseModel):
    title: str | None = None


class DocumentUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    generated_spans: list[DocumentSpan] | None = None

    @model_validator(mode="after")
    def _spans_need_content(self) -> DocumentUpdate:
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


class DocumentAuditRequest(BaseModel):
    # The generated run to audit/patch. `context` is the FULL document text that
    # preceded the run — i.e. the generation prompt. /patch byte-extends it so
    # the server's KV prefix survives; the scanners get a server-side cap.
    draft: str
    context: str = ""
    # Same flag as DocumentGenerateRequest — drives the context-scrubbing
    # heuristic (assisted note macros vs raw template markers).
    assisted: bool = False
    # True when the run ended early (Stop, or finish == "length"); the server
    # trims the dangling partial sentence before auditing/patching.
    truncated: bool = False


class AuditReportPayload(BaseModel):
    # Serialized AuditReport (analysis.report_to_dict): one `sections` entry per
    # scanner with findings, keyed by its AUDIT_TYPES name.
    total_issues: int
    is_clean: bool
    sections: dict[str, Any]


class DocumentAuditResponse(BaseModel):
    report: AuditReportPayload
    # "no_complete_sentence" when a truncated draft had nothing auditable left
    # after trimming (the /patch route also uses "clean": nothing to fix).
    skipped: str | None = None
    # True when a truncated tail fragment was excluded from the scan.
    tail_excluded: bool = False


class DocumentPatchResponse(BaseModel):
    # Full replacement text for the run (patched core + any truncated tail
    # fragment reattached verbatim). Unchanged text when nothing applied.
    patched_draft: str
    patch_count: int
    errors: list[str] = []
    report_after: AuditReportPayload
    skipped: str | None = None


class CharacterCardCreate(BaseModel):
    # id and source_format are normally omitted (manual creation). They are
    # supplied by the import flow: /api/characters/import parses the PNG and
    # computes a stable deterministic ID (orb_id embedded in the card, or a
    # SHA-256-derived UUID of the raw bytes), then the frontend passes it back
    # here on Save. Preserving the original ID means re-importing a card after
    # deletion relinks its conversation history instead of creating an orphan.
    id: str | None = None
    source_format: str | None = None
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
    avatar_b64: str | None = None
    avatar_mime: str | None = None
    world_id: str | None = None
    character_book: dict | None = None
    # V2 card extensions dict, stored verbatim (third-party keys round-trip
    # through export). Orb's card-embedded fragments live at orb.fragments;
    # card_embedded_fragments() validates that subtree on consumption.
    extensions: dict | None = None


class CharacterCardUpdate(BaseModel):
    name: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, v: str | None) -> str | None:
        if v is not None:
            stripped = v.strip()
            if not stripped:
                raise ValueError("name must not be empty or whitespace-only")
            return stripped
        return v

    personality: str | None = None
    scenario: str | None = None
    first_mes: str | None = None
    mes_example: str | None = None
    creator_notes: str | None = None
    system_prompt: str | None = None
    post_history_instructions: str | None = None
    tags: list[str] | None = None
    creator: str | None = None
    alternate_greetings: list[str] | None = None
    avatar_b64: str | None = None
    avatar_mime: str | None = None
    world_id: str | None = None
    extensions: dict | None = None
    # Persona lock for this character card; an explicit null clears it (handled
    # via model_fields_set in api_update_character since the route drops Nones).
    persona_lock_id: int | None = None


class AttachmentIn(BaseModel):
    b64: str
    mime: str
    filename: str | None = None
    size: int | None = None

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
    turn_index: int | None = None
    attachments: list[AttachmentIn] = []


class EditMessage(BaseModel):
    content: str
    enable_agent: bool = True
    attachments: list[AttachmentIn] = []


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
    avatar_color: str | None = None


class UserPersonaUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    avatar_color: str | None = None


class ResetConfirm(BaseModel):
    confirm: bool


class CleanupRequest(BaseModel):
    """Age-based data cleanup. ``days=0`` means "everything, regardless of age"."""

    artifacts: bool = False
    logs: bool = False
    days: int = Field(default=0, ge=0)


class ImportUrlRequest(BaseModel):
    source: str
    full_path: str


class PresetExportRequest(BaseModel):
    domains: list[str]
    strip_keys: bool = True
    label: str = ""


class ExtensionInstallRequest(BaseModel):
    """Apply a staged install.

    ``token`` is the opaque staging token from the matching inspect call; it,
    not this body, decides *which* package installs. ``permissions`` echoes back
    the exact normalized permission values the user approved -- the server keeps
    only entries the active manifest actually requests, so the frontend can
    never widen a grant by reconstructing one from a display string.
    """

    token: str
    permissions: list[dict] = Field(default_factory=list)
    enabled: bool = True


class ExtensionUpdateRequest(BaseModel):
    """Apply a staged update or rollback. Same token/echo contract as install."""

    token: str
    permissions: list[dict] = Field(default_factory=list)


class ExtensionPermissionsUpdate(BaseModel):
    # Required (no default): a body lacking "permissions" is a 422, never an
    # accidental revoke-everything.
    permissions: list[dict]


class ExtensionPurgeRequest(BaseModel):
    """Two-phase purge. Omit ``token`` for the count preview; send the token the
    preview returned to actually delete."""

    token: str | None = None
