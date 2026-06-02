"""Domain data contracts owned by the database (the model layer).

These describe the *shape* of persisted data and depend on nothing else in the
codebase, so every other layer can point its dependencies inward, toward the
data — never the reverse. Anything in backend/database/ that reaches "up" into
passes/ or the orchestrator for a shared shape is an architectural inversion;
put the shape here instead.
"""

from __future__ import annotations

from typing import Literal, TypedDict, Union

# A phrase-bank group is either a legacy list of literal variant strings, or a
# {"kind": "literal"|"regex", ...} dict. The matching semantics that consume
# this shape live in backend/passes/editor/slop_detector.py.
PhraseGroup = Union[list[str], dict]


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


class SettingsRow(TypedDict, total=False):
    """The merged settings dict returned by ``get_settings()``.

    ``total=False`` on purpose: the dict is assembled from three sources and no
    single key is guaranteed present everywhere --
      1. the ``SELECT *`` row off the ``settings`` table,
      2. the ``DEFAULT_SETTINGS`` fallback (seeds.py) when no row exists, which
         omits several keys, and
      3. the agent-endpoint cascade, which overlays the ``agent_*`` /
         ``endpoint_url`` extras only when an active model config resolves.
    So this catches key *typos* and value-*type* mismatches without falsely
    asserting presence. The write side of the same table is the Pydantic
    ``SettingsUpdate`` in backend/main.py -- keep the two in sync.
    """

    # Persisted scalar columns (schema.py: settings).
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
    active_persona_id: int | None
    active_endpoint_id: int | None
    character_library_view: str
    character_library_sort: str
    show_editor_diff: int
    hide_streaming_until_baked: int
    prevent_prompt_overrides: int
    agent_same_as_writer: bool
    agent_endpoint_id: int | None
    agent_shared_system_prompt: str
    attachment_cache_budget_bytes: int
    attachment_access_counter: int
    # JSON columns, decoded to their in-memory shape by get_settings().
    enabled_tools: dict
    reasoning_enabled_passes: dict
    inspector_open_states: dict
    editor_audit_toggles: dict
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


class WorkflowAttachmentRow(TypedDict, total=False):
    """A row from ``workflow_attachments`` (schema.py)."""

    id: int
    message_id: int
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
