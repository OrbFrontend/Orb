"""Domain data contracts owned by the database (the model layer).

These describe the *shape* of persisted data and depend on nothing else in the
codebase, so every other layer can point its dependencies inward, toward the
data — never the reverse. Anything in backend/database/ that reaches "up" into
the pipeline, analysis, or any other higher layer for a shared shape is an
architectural inversion; put the shape here instead.
"""

from __future__ import annotations

from typing import Literal, TypedDict, Union


# A phrase-bank group is one of three shapes. ``get_phrase_bank()`` emits the
# two ``{"kind": ...}`` dicts; the bare ``list[str]`` is a legacy literal group
# still accepted by the detector for backwards compatibility. The matching
# semantics that consume these shapes live in
# backend/analysis/detectors/slop_detector.py.
class LiteralPhraseGroup(TypedDict):
    """A set of equivalent literal variant phrases."""

    kind: Literal["literal"]
    variants: list[str]


class RegexPhraseGroup(TypedDict):
    """A single regex, matched case-insensitively against each sentence."""

    kind: Literal["regex"]
    pattern: str


PhraseGroup = Union[list[str], LiteralPhraseGroup, RegexPhraseGroup]


class PhraseBankRow(TypedDict):
    """A ``phrase_bank`` row as get_phrase_bank_rows() exposes it for UI
    management -- distinct from :data:`PhraseGroup` (the detector-facing shape).
    ``variants`` is the JSON-*decoded* list; ``kind`` is normalised to
    ``"literal"`` when the column is NULL, and ``pattern`` to ``""``.
    """

    id: int
    kind: str
    variants: list[str]
    pattern: str


# ── Row contracts ──────────────────────────────────────────────────────────
#
# These TypedDicts label the plain dicts the query layer fetches from SQLite
# (``dict(row)``), so callers' ``row["key"]`` access is checked against the
# schema with *zero* runtime change -- the rows stay ordinary dicts. They are
# introduced at the query boundary with ``cast(...)`` (a TypedDict is not
# assignable from a bare ``dict``). Add tables here one at a time; mirror the
# columns in backend/database/schema.py. JSON-encoded columns are typed as the
# *decoded* shape (``dict``/``list``) only on the queries that actually decode
# them -- see the per-field notes below.


class _SettingsBase(TypedDict):
    """The keys ``get_settings()`` *always* returns, in either branch -- the
    ``DEFAULT_SETTINGS`` fallback (seeds.py) supplies exactly this set, and the
    ``SELECT *`` branch supplies them too (every one is a persisted column or a
    field the query unconditionally sets). Splitting these out as a ``total=True``
    base lets readers subscript them (``settings["endpoint_url"]``) without a
    not-required-access warning, while genuinely-conditional keys stay optional
    on ``SettingsRow`` below. Keep this set in lockstep with ``DEFAULT_SETTINGS``.
    """

    endpoint_url: str
    api_key: str
    model_name: str
    temperature: float
    min_p: float
    top_k: int
    top_p: float
    repetition_penalty: float
    max_tokens: int
    shared_system_prompt: str
    system_prompt: str
    user_name: str
    user_description: str
    enable_agent: bool
    length_guard_max_words: int
    length_guard_max_paragraphs: int
    length_guard_enabled: int
    length_guard_enforce: int
    agentic_lorebook_enabled: int
    character_library_view: str
    character_library_sort: str
    show_editor_diff: int
    editor_audit_toggles: dict  # decoded to its in-memory shape by get_settings()
    hide_streaming_until_baked: int
    prevent_prompt_overrides: int
    agent_same_as_writer: bool
    agent_shared_system_prompt: str
    feedback_enabled: int


class SettingsRow(_SettingsBase, total=False):
    """The merged settings dict returned by ``get_settings()``.

    The always-present keys live on :class:`_SettingsBase`; the keys here are
    ``total=False`` because they are *not* guaranteed present --
      1. several columns / JSON fields appear only on the ``SELECT *`` branch and
         are omitted by the ``DEFAULT_SETTINGS`` fallback, and
      2. the agent-endpoint cascade overlays the ``agent_*`` / ``endpoint_url``
         extras only when an active model config resolves.
    So this catches key *typos* and value-*type* mismatches without falsely
    asserting presence. The write side of the same table is the Pydantic
    ``SettingsUpdate`` in backend/main.py -- keep the two in sync.
    """

    # Columns present on the SELECT * branch but omitted by DEFAULT_SETTINGS.
    active_persona_id: int | None
    active_endpoint_id: int | None
    agent_endpoint_id: int | None
    attachment_cache_budget_bytes: int
    attachment_access_counter: int
    generated_chars: int | None
    # JSON columns, decoded to their in-memory shape by get_settings() on the
    # SELECT * branch only (DEFAULT_SETTINGS omits them).
    enabled_tools: dict[str, bool]
    reasoning_enabled_passes: dict
    inspector_open_states: dict
    workflow_config: str  # left raw; decoded per-slot by get_workflow_config()
    # Agent-endpoint cascade overlays (present only when it resolves).
    agent_endpoint_url: str
    agent_api_key: str
    agent_model_name: str
    agent_temperature: float
    agent_min_p: float
    agent_top_k: int
    agent_top_p: float
    agent_repetition_penalty: float
    agent_max_tokens: int
    agent_system_prompt: str


class ConversationRow(TypedDict):
    """A row from the ``conversations`` table (schema.py).

    ``workflow_state`` is left as the raw JSON string; it is decoded per-slot by
    get_workflow_state(), not eagerly here.
    """

    id: str
    title: str
    character_card_id: str | None
    character_name: str
    character_scenario: str
    post_history_instructions: str
    created_at: str
    updated_at: str | None
    active_leaf_id: int | None
    workflow_state: str | None
    persona_lock_id: int | None


class ConversationListRow(ConversationRow, total=False):
    """A ``ConversationRow`` plus the two aggregate columns list_conversations()
    selects for the sidebar. ``total=False`` because they exist only on that
    query's rows, not on the base table.
    """

    last_message_preview: str | None
    message_count: int


class MessageRow(TypedDict):
    """A row from the ``messages`` table.

    NOTE: ``progressive_fields`` is the JSON-*decoded* dict, which is how
    get_path_to_leaf()/get_messages() expose it. ``get_message_by_id()`` does a
    plain ``dict(row)`` and leaves it as the raw JSON *string* -- a pre-existing
    inconsistency this label makes visible rather than fixes.
    """

    id: int
    conversation_id: str
    role: Literal["user", "assistant"]
    content: str
    turn_index: int
    parent_id: int | None
    progressive_fields: dict
    created_at: str
    workflow_state: str | None


class UserAttachmentRow(TypedDict, total=False):
    """A row from ``user_attachments`` (schema.py)."""

    id: int
    message_id: int
    mime_type: str
    data_b64: str
    filename: str | None
    size: int | None
    created_at: str


class WorkflowAttachmentRowBase(TypedDict):
    """The columns every ``workflow_attachments`` reader projects.

    ``get_workflow_attachments_for_message()`` filters by ``message_id`` and
    omits that redundant column, so it returns this base directly; the
    single-row reader and the per-message attachment glue also project
    ``message_id`` and return the fuller :class:`WorkflowAttachmentRow`. Split
    out as a ``total=True`` base so those full-row readers can require the FK
    (consumers subscript it) while the projection reader stays honest. Mirrors
    the ``_SettingsBase`` / :class:`SettingsRow` split. ``data_b64`` is the
    EVICTED_MARKER sentinel string once an artifact's bytes are evicted -- see
    secondary-workflow.md §9.
    """

    id: int
    mime_type: str
    data_b64: str
    filename: str | None
    created_at: str
    workflow_id: str
    parent_attachment_id: int | None
    annotation: str | None
    seed: str | None
    generation_metadata: str | None
    consumption_metadata: str | None
    active_sibling_id: int | None
    recent_accesses: str | None


class WorkflowAttachmentRow(WorkflowAttachmentRowBase):
    """A fully-projected ``workflow_attachments`` row -- the shared columns plus
    the ``message_id`` FK -- as get_workflow_attachment_by_id() and the
    per-message attachment glue return it.
    """

    message_id: int


class MessageWithAttachments(MessageRow, total=False):
    """A ``MessageRow`` after the query layer glues on related rows and branch
    navigation metadata in place. The extra keys are not columns; they are
    populated by _attach_attachments() and get_messages_with_branch_info(),
    hence ``total=False``.
    """

    user_attachments: list[UserAttachmentRow]
    workflow_attachments: list[WorkflowAttachmentRow]
    branch_count: int
    branch_index: int
    prev_branch_id: int | None
    next_branch_id: int | None


# NOTE on the ``int`` columns below: SQLite has no boolean type. Columns the
# schema declares ``BOOLEAN`` / flags (enabled, required, case_insensitive,
# constant, ...) come back from ``dict(row)`` as 0/1 ints, so they are typed
# ``int`` to match the runtime value, not ``bool``.


class EndpointRow(TypedDict):
    """A row from the ``endpoints`` table. Every query selects exactly these
    five columns (avatar/secret columns are never projected here)."""

    id: int
    url: str
    api_key: str
    active_model_config_id: int | None
    agent_active_model_config_id: int | None


class ModelConfigRow(TypedDict):
    """A row from the ``model_configs`` table (``SELECT *``)."""

    id: int
    endpoint_id: int
    model_name: str
    system_prompt: str
    temperature: float
    min_p: float
    top_k: int
    top_p: float
    repetition_penalty: float
    max_tokens: int
    role: Literal["writer", "agent"]


class WorldRow(TypedDict):
    """A row from the ``worlds`` table (``SELECT *``)."""

    id: str
    name: str
    enabled: int
    created_at: str
    updated_at: str


class LorebookEntryRow(TypedDict):
    """A row from ``lorebook_entries``. ``keywords`` is the JSON-*decoded* list
    (every reader runs it through _parse_lorebook_entry / an inline decode)."""

    id: int
    world_id: str
    name: str
    content: str
    keywords: list
    case_insensitive: int
    constant: int
    priority: int
    enabled: int
    sort_order: int
    created_at: str
    updated_at: str


class ActiveLorebookEntryRow(LorebookEntryRow):
    """A :class:`LorebookEntryRow` joined with its world's name, as
    ``get_active_lorebook_entries()`` returns it (it selects ``w.name AS
    world_name`` on top of ``le.*``). Required-base + extension idiom: the
    single-entry readers project only ``le.*`` and return the base, while this
    join reader projects a strict superset and adds ``world_name`` (used to group
    the Director's agentic-lorebook catalog by world)."""

    world_name: str


class UserPersonaRow(TypedDict):
    """A row from ``user_personas`` (the queries select these six columns)."""

    id: int
    name: str
    description: str
    avatar_color: str | None
    created_at: str
    updated_at: str


class InteractiveFragmentRow(TypedDict):
    """A row from ``interactive_fragments`` (``SELECT *``)."""

    id: str
    label: str
    description: str
    field_type: str
    required: int
    enabled: int
    injection_label: str
    sort_order: int


class MoodFragmentRow(TypedDict):
    """A row from ``mood_fragments`` (``SELECT *``)."""

    id: str
    label: str
    description: str
    prompt_text: str
    negative_prompt: str
    enabled: int


class DirectorStateRow(TypedDict):
    """The director-state dict returned by ``get_director_state()``.

    The JSON columns are decoded before return: ``active_moods`` and
    ``keywords`` to lists, ``progressive_fields`` to a dict. When no row exists
    the query synthesizes the same shape with empty containers.
    """

    conversation_id: str
    active_moods: list
    keywords: list
    progressive_fields: dict


class ConversationLogRow(TypedDict):
    """A ``conversation_logs`` row as get_conversation_logs() /
    get_director_log_for_message() expose it.

    ``tool_calls`` and ``active_moods_after`` are JSON-*decoded* to lists;
    ``progressive_fields_after`` is left as the raw JSON string (neither reader
    decodes it). The nullable TEXT/INTEGER columns come back ``None`` when unset
    -- get_director_log_for_message() additionally defaults the ``reasoning_*``
    keys to ``""``, but get_conversation_logs() leaves them as stored.
    ``feedback`` is the JSON-*decoded* dict (the editor feedback sub-step's
    user-facing note); both readers decode it and ``setdefault`` it for
    pre-feature rows, mirroring the reasoning fields. (Feedback shares the
    editor's reasoning/latency, so it has no columns of its own for those.)
    """

    id: int
    conversation_id: str
    turn_index: int
    agent_raw_output: str | None
    tool_calls: list
    active_moods_after: list
    progressive_fields_after: str
    injection_block: str | None
    agent_latency_ms: int | None
    created_at: str
    message_id: int | None
    reasoning_director: str | None
    reasoning_writer: str | None
    reasoning_editor: str | None
    feedback: dict


class CharacterCardRow(TypedDict, total=False):
    """A row from ``character_cards``.

    ``total=False`` because the readers project different column subsets:
    ``list_character_cards`` returns only the lightweight columns the library
    list consumes — it drops ``avatar_mime`` (deriving ``has_avatar``) and also
    omits the heavy text bodies (``description``, ``personality``, ``scenario``,
    ``first_mes``, ``system_prompt``) that no list consumer reads, to keep a
    large library's payload small; ``get_character_card`` returns the full row
    (and includes ``avatar_b64`` only when ``include_avatar``).
    ``tags`` and ``alternate_greetings`` are the JSON-*decoded* lists;
    ``has_avatar`` is a derived bool, not a column.
    """

    id: str
    name: str
    description: str
    personality: str
    scenario: str
    first_mes: str
    mes_example: str
    creator_notes: str
    system_prompt: str
    post_history_instructions: str
    tags: list
    creator: str
    character_version: str
    alternate_greetings: list
    avatar_b64: str | None
    avatar_mime: str | None
    source_format: str
    world_id: str | None
    created_at: str
    updated_at: str
    workflow_state: str | None
    persona_lock_id: int | None
    has_avatar: bool
