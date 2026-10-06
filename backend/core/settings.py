"""The merged settings view, shared by every layer that reads settings.

``database.get_settings()`` builds it; the pipeline, prompting, inference, features and workflows read it. It lives here, below
all of them, because each of those layers must be able to name it. Every item is ReadOnly: settings change only through the
settings queries, so a reader that writes into its copy (or into a plug-in's read-only view) is a type error.
"""

from __future__ import annotations

from typing import TypedDict

from typing_extensions import ReadOnly

from .domain_types import CompletionMode


class ConnectionSettings(TypedDict):
    """Not settings columns: overlaid from the active endpoint and its active Writer model config, with fixed fallbacks when
    neither is selected."""

    endpoint_url: ReadOnly[str]
    api_key: ReadOnly[str]
    model_name: ReadOnly[str]
    temperature: ReadOnly[float | None]
    min_p: ReadOnly[float | None]
    top_k: ReadOnly[int | None]
    top_p: ReadOnly[float | None]
    repetition_penalty: ReadOnly[float | None]
    max_tokens: ReadOnly[int | None]


class _SettingsBase(ConnectionSettings):
    """Keys always returned by get_settings, including DEFAULT_SETTINGS fallback.

    DEFAULT_SETTINGS is annotated with this type's subclass, so the type checker keeps the two in sync; conditional keys belong
    on Settings.
    """

    shared_system_prompt: ReadOnly[str]
    system_prompt: ReadOnly[str]  # the active Writer model config's system_prompt replaces the column's value
    user_name: ReadOnly[str]
    user_description: ReadOnly[str]
    enable_agent: ReadOnly[int]
    length_guard_max_words: ReadOnly[int]
    length_guard_max_paragraphs: ReadOnly[int]
    length_guard_enabled: ReadOnly[int]
    length_guard_enforce: ReadOnly[int]
    agentic_lorebook_enabled: ReadOnly[int]
    character_library_view: ReadOnly[str]
    character_library_sort: ReadOnly[str]
    show_editor_diff: ReadOnly[int]
    show_chat_avatars: ReadOnly[int]
    inspector_inline: ReadOnly[int]
    editor_audit_toggles: ReadOnly[dict]  # decoded to its in-memory shape by get_settings()
    document_audit_enabled: ReadOnly[int]
    document_audit_autopatch: ReadOnly[int]
    document_audit_toggles: ReadOnly[dict]  # decoded by get_settings(); doc-applicable scanner subset only
    hide_streaming_until_baked: ReadOnly[int]
    expression_rendering: ReadOnly[str]
    prevent_prompt_overrides: ReadOnly[int]
    agent_same_as_writer: ReadOnly[bool]
    agent_shared_system_prompt: ReadOnly[str]
    director_individual_fragments: ReadOnly[int]
    workflows_globally_enabled: ReadOnly[int]


class Settings(_SettingsBase, total=False):
    """The merged settings every layer reads: the settings row, decoded, with the active endpoints and model configs overlaid.

    Guaranteed keys come from _SettingsBase; optional keys cover SELECT-only fields and resolved agent-endpoint overlays. Keep
    types in sync with the API SettingsUpdate contract. A function that reads settings takes this type, never a bare mapping,
    so a misspelled key or a wrong value type is a type error rather than a silent default.
    """

    # Columns present on the SELECT * branch but omitted by DEFAULT_SETTINGS.
    active_persona_id: ReadOnly[int | None]
    active_endpoint_id: ReadOnly[int | None]
    agent_endpoint_id: ReadOnly[int | None]
    # Decision classifier configuration. ``decision_endpoint_id`` is None until the user saves a judge endpoint; until then
    # enabled decisions are skipped and make no request.
    decision_endpoint_id: ReadOnly[int | None]
    decision_model: ReadOnly[str]
    attachment_cache_budget_bytes: ReadOnly[int]
    attachment_access_counter: ReadOnly[int]
    generated_chars: ReadOnly[int | None]
    # JSON columns, decoded to their in-memory shape by get_settings() on the
    # SELECT * branch only (DEFAULT_SETTINGS omits them).
    enabled_tools: ReadOnly[dict[str, bool]]
    reasoning_enabled_passes: ReadOnly[dict]
    reasoning_prefill_passes: ReadOnly[dict]
    inspector_open_states: ReadOnly[dict]
    workflow_config: ReadOnly[str]  # left raw; decoded per-slot by get_workflow_config()
    workflow_enabled: ReadOnly[dict[str, bool]]  # decoded by get_settings(); per-workflow on/off, missing key => on
    local_ml_enabled: ReadOnly[dict[str, bool]]  # decoded by get_settings(); per-local-ML-feature on/off, missing key => on
    # Per-local-ML-feature config, decoded by get_settings(). Sibling to local_ml_enabled and written only by the dedicated
    # route, never by update_settings(). Shape is the feature's own, e.g. {"prose_rewriter": {"variant": "4b-q8", "gpu": true,
    # "batch_size": 2}}.
    local_ml_config: ReadOnly[dict[str, dict]]
    # Per-endpoint transport mode, surfaced by the get_settings() overlay from the active/agent endpoint row (default 'chat').
    # agent_completion_mode falls back to completion_mode when the agent shares the writer endpoint.
    completion_mode: ReadOnly[CompletionMode]
    agent_completion_mode: ReadOnly[CompletionMode]
    # Per-endpoint proxy URL, surfaced by the same overlay (default ''); empty means a direct connection. agent_proxy falls back
    # to proxy when the agent shares the writer endpoint.
    proxy: ReadOnly[str]
    agent_proxy: ReadOnly[str]
    # Per-model reasoning effort, surfaced by the same overlay (default ''); empty means no effort param is sent and the
    # provider default governs. 'custom' sends {reasoning_effort_param: reasoning_effort_value} instead of the standard param.
    # The agent_* variants fall back to the writer's values when the agent shares the writer endpoint.
    reasoning_effort: ReadOnly[str]
    reasoning_effort_param: ReadOnly[str]
    reasoning_effort_value: ReadOnly[str]
    agent_reasoning_effort: ReadOnly[str]
    agent_reasoning_effort_param: ReadOnly[str]
    agent_reasoning_effort_value: ReadOnly[str]
    # Arbitrary per-model request additions, surfaced by the same overlay (default ''). extra_headers is "Name: value" lines
    # merged into the outbound headers; extra_body is a JSON object merged into the chat body. The agent_* variants fall back to
    # the writer's values when the agent shares the writer endpoint.
    extra_headers: ReadOnly[str]
    extra_body: ReadOnly[str]
    agent_extra_headers: ReadOnly[str]
    agent_extra_body: ReadOnly[str]
    # Agent-endpoint cascade overlays (present only when it resolves).
    agent_endpoint_url: ReadOnly[str]
    agent_api_key: ReadOnly[str]
    agent_model_name: ReadOnly[str]
    agent_temperature: ReadOnly[float | None]
    agent_min_p: ReadOnly[float | None]
    agent_top_k: ReadOnly[int | None]
    agent_top_p: ReadOnly[float | None]
    agent_repetition_penalty: ReadOnly[float | None]
    agent_max_tokens: ReadOnly[int | None]
    agent_system_prompt: ReadOnly[str]
