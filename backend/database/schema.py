from __future__ import annotations

import re

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    endpoint_url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL,
    temperature REAL NOT NULL DEFAULT 0.8,
    min_p REAL NOT NULL DEFAULT 0.05,
    top_k INTEGER NOT NULL DEFAULT 40,
    top_p REAL NOT NULL DEFAULT 0.95,
    repetition_penalty REAL NOT NULL DEFAULT 1.0,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    shared_system_prompt TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    user_name TEXT NOT NULL DEFAULT 'User',
    user_description TEXT NOT NULL DEFAULT '',
    enabled_tools TEXT NOT NULL DEFAULT '{}',
    enable_agent INTEGER NOT NULL DEFAULT 1,
    length_guard_max_words INTEGER NOT NULL DEFAULT 240,
    length_guard_max_paragraphs INTEGER NOT NULL DEFAULT 4,
    length_guard_enabled INTEGER NOT NULL DEFAULT 0,
    length_guard_enforce INTEGER NOT NULL DEFAULT 0,
    agentic_lorebook_enabled INTEGER NOT NULL DEFAULT 0,
    reasoning_enabled_passes TEXT NOT NULL DEFAULT '{"director":false,"writer":false,"editor":false}',
    reasoning_prefill_passes TEXT NOT NULL DEFAULT '{"director":"","writer":"","editor":""}',
    active_persona_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL,
    active_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL,
    character_library_view TEXT NOT NULL DEFAULT 'grid',
    character_library_sort TEXT NOT NULL DEFAULT 'time-added',
    show_editor_diff INTEGER NOT NULL DEFAULT 1,
    editor_audit_toggles TEXT NOT NULL DEFAULT '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,"contrastive_negation":true,"phrase_repetition":true,"structural_repetition":true,"anti_echo":true}',
    document_audit_enabled INTEGER NOT NULL DEFAULT 1,
    document_audit_autopatch INTEGER NOT NULL DEFAULT 0,
    document_audit_toggles TEXT NOT NULL DEFAULT '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,"contrastive_negation":true}',
    hide_streaming_until_baked INTEGER NOT NULL DEFAULT 0,
    prevent_prompt_overrides INTEGER NOT NULL DEFAULT 0,
    agent_same_as_writer INTEGER NOT NULL DEFAULT 1,
    agent_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL,
    agent_shared_system_prompt TEXT NOT NULL DEFAULT '',
    feedback_enabled INTEGER NOT NULL DEFAULT 0,
    director_individual_fragments INTEGER NOT NULL DEFAULT 0,
    direction_notes_record INTEGER NOT NULL DEFAULT 0,
    direction_notes_inject TEXT NOT NULL DEFAULT 'off',
    inspector_open_states TEXT NOT NULL DEFAULT '{"reasoning":true,"tool_calls":false,"injection_block":false,"context_size":true}',
    workflow_config TEXT NOT NULL DEFAULT '{}',
    workflows_globally_enabled INTEGER NOT NULL DEFAULT 1,
    workflow_enabled TEXT NOT NULL DEFAULT '{}',
    local_ml_enabled TEXT NOT NULL DEFAULT '{}',
    attachment_cache_budget_bytes INTEGER NOT NULL DEFAULT 524288000,
    attachment_access_counter INTEGER NOT NULL DEFAULT 0,
    generated_chars INTEGER DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS mood_fragments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    negative_prompt TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'New Conversation',
    character_card_id TEXT DEFAULT NULL,
    character_name TEXT NOT NULL DEFAULT '',
    character_scenario TEXT NOT NULL DEFAULT '',
    post_history_instructions TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT,
    last_accessed_at TEXT,
    active_leaf_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    workflow_state TEXT DEFAULT NULL,
    persona_lock_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL,
    macro_seed TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS character_cards (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    personality TEXT NOT NULL DEFAULT '',
    scenario TEXT NOT NULL DEFAULT '',
    first_mes TEXT NOT NULL DEFAULT '',
    mes_example TEXT NOT NULL DEFAULT '',
    creator_notes TEXT NOT NULL DEFAULT '',
    system_prompt TEXT NOT NULL DEFAULT '',
    post_history_instructions TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    creator TEXT NOT NULL DEFAULT '',
    character_version TEXT NOT NULL DEFAULT '',
    alternate_greetings TEXT NOT NULL DEFAULT '[]',
    avatar_b64 TEXT DEFAULT NULL,
    avatar_mime TEXT DEFAULT NULL,
    source_format TEXT NOT NULL DEFAULT 'manual',
    world_id TEXT DEFAULT NULL REFERENCES worlds(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    workflow_state TEXT DEFAULT NULL,
    persona_lock_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL,
    extensions TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS character_expressions (
    character_card_id TEXT NOT NULL REFERENCES character_cards(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    data_b64 TEXT NOT NULL,
    mime TEXT NOT NULL DEFAULT 'image/png',
    PRIMARY KEY (character_card_id, label)
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    parent_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    progressive_fields TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    workflow_state TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS director_state (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    active_moods TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]',
    progressive_fields TEXT NOT NULL DEFAULT '{}',
    macro_choices TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS interactive_fragments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    field_type TEXT NOT NULL DEFAULT 'string',
    required BOOLEAN NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    injection_label TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0,
    direction_note_timing TEXT NOT NULL DEFAULT 'post_turn',
    type_config TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS conversation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    tool_calls TEXT,
    active_moods_after TEXT,
    injection_block TEXT,
    agent_latency_ms INTEGER,
    created_at TEXT NOT NULL,
    message_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    reasoning_director TEXT,
    reasoning_writer TEXT,
    reasoning_editor TEXT,
    feedback TEXT NOT NULL DEFAULT '{}',
    fragment_diagnostics TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS phrase_bank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    variants TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'literal',
    pattern TEXT
);

CREATE TABLE IF NOT EXISTS user_personas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar_color TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    mime_type TEXT NOT NULL,
    data_b64 TEXT NOT NULL,
    filename TEXT,
    size INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    mime_type TEXT NOT NULL,
    data_b64 TEXT NOT NULL,
    filename TEXT,
    created_at TEXT NOT NULL,
    workflow_id TEXT NOT NULL,
    parent_attachment_id INTEGER REFERENCES workflow_attachments(id) ON DELETE CASCADE,
    annotation TEXT DEFAULT NULL,
    seed TEXT DEFAULT NULL,
    generation_metadata TEXT DEFAULT NULL,
    consumption_metadata TEXT DEFAULT NULL,
    active_sibling_id INTEGER REFERENCES workflow_attachments(id) ON DELETE SET NULL,
    recent_accesses TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL,
    agent_active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL,
    completion_mode TEXT NOT NULL DEFAULT 'chat' CHECK (completion_mode IN ('chat', 'text')),
    proxy TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS model_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    temperature REAL NOT NULL DEFAULT 0.8,
    min_p REAL NOT NULL DEFAULT 0.0,
    top_k INTEGER NOT NULL DEFAULT 40,
    top_p REAL NOT NULL DEFAULT 0.95,
    repetition_penalty REAL NOT NULL DEFAULT 1.0,
    max_tokens INTEGER NOT NULL DEFAULT 4096,
    role TEXT NOT NULL DEFAULT 'writer' CHECK (role IN ('writer', 'agent')),
    reasoning_effort TEXT NOT NULL DEFAULT '',
    reasoning_effort_param TEXT NOT NULL DEFAULT '',
    reasoning_effort_value TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS worlds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lorebook_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '[]',
    case_insensitive BOOLEAN NOT NULL DEFAULT 1,
    constant BOOLEAN NOT NULL DEFAULT 0,
    use_regex INTEGER NOT NULL DEFAULT 0,
    selective INTEGER NOT NULL DEFAULT 0,
    secondary_keys TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS direction_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    interactive_fragment_id TEXT NOT NULL DEFAULT '',
    interactive_fragment_label TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dirnote_message ON direction_notes(message_id);
CREATE INDEX IF NOT EXISTS idx_dirnote_conversation ON direction_notes(conversation_id);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Untitled',
    content TEXT NOT NULL DEFAULT '',
    generated_spans TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Community extensions. These three tables are LOCAL_ONLY (see
-- database/preset_schema.py): they ride along in a full local snapshot so a
-- rollback also rolls back installation metadata, and are stripped from every
-- shareable preset. Installing an extension is a per-machine trust decision,
-- so a preset must never be able to carry one to another user's Orb.
CREATE TABLE IF NOT EXISTS extension_packages (
    id TEXT PRIMARY KEY,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('git', 'archive')),
    source_url TEXT NOT NULL DEFAULT '',
    requested_ref TEXT NOT NULL DEFAULT '',
    active_digest TEXT NOT NULL,
    previous_digest TEXT DEFAULT NULL,
    approved_permissions TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    load_status TEXT NOT NULL DEFAULT 'available'
        CHECK (load_status IN ('available', 'incompatible', 'invalid', 'missing_content')),
    load_error TEXT NOT NULL DEFAULT '',
    -- Local-only selection of the one active Writer resolver. At most one row
    -- may carry a 1; the write path clears every other row in the same
    -- transaction, so "which resolver is active" is never a question two rows
    -- can answer differently. It is a *preference*, not an eligibility fact:
    -- disabling or revoking makes it inactive immediately while the value
    -- survives, so re-enabling restores what the user chose. Uninstall drops
    -- it with the row, so a later package claiming the same id is not silently
    -- activated. The whole table is LOCAL_ONLY, so it never travels in a
    -- shareable preset either.
    writer_tool_active INTEGER NOT NULL DEFAULT 0,
    installed_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- One row per content digest Orb has compiled for a package. Keeping the
-- manifest and resolved commit here (rather than only two digest strings on
-- the package row) is what lets rollback recompile the prior revision and show
-- an honest permission diff instead of guessing.
CREATE TABLE IF NOT EXISTS extension_revisions (
    extension_id TEXT NOT NULL REFERENCES extension_packages(id) ON DELETE CASCADE,
    content_digest TEXT NOT NULL,
    manifest TEXT NOT NULL,
    extension_api INTEGER NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    commit_id TEXT DEFAULT NULL,
    contract_fingerprint TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    PRIMARY KEY (extension_id, content_digest)
);

-- Write-only from the API's perspective: reads return presence metadata, never
-- the value. At-rest storage is Orb's ordinary local SQLite posture -- the
-- security property here is non-disclosure to package logic and to frontend
-- payloads, not encryption Orb does not provide.
CREATE TABLE IF NOT EXISTS extension_secrets (
    extension_id TEXT NOT NULL REFERENCES extension_packages(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    secret_value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (extension_id, name)
);

"""


def table_create_sql(table: str) -> str:
    """Return the ``CREATE TABLE IF NOT EXISTS <table> ( ... )`` block for *table*,
    sliced out of ``CREATE_TABLES_SQL``.

    This is the single source of truth for a table's canonical fresh-install shape.
    Rebuild migrations (e.g. 0027) and the schema-equivalence gate both derive the
    canonical DDL from here rather than pasting a copy, so a rebuild can never drift
    from the shape the equivalence check enforces. Parentheses are balanced (column
    ``REFERENCES`` and ``CHECK`` clauses nest), so the block ends at the matching
    close paren, not the first one.
    """
    m = re.search(rf"CREATE TABLE IF NOT EXISTS {re.escape(table)}\s*\(", CREATE_TABLES_SQL)
    if not m:
        raise KeyError(f"no CREATE TABLE block for {table!r} in CREATE_TABLES_SQL")
    depth = 0
    for i in range(m.end() - 1, len(CREATE_TABLES_SQL)):
        ch = CREATE_TABLES_SQL[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return CREATE_TABLES_SQL[m.start() : i + 1]
    raise ValueError(f"unbalanced parentheses extracting {table!r} from CREATE_TABLES_SQL")
