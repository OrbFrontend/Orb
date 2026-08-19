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
    macro_seed TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT 'solo' CHECK (kind IN ('solo', 'group')),
    group_turn_mode TEXT NOT NULL DEFAULT 'director' CHECK (group_turn_mode IN ('manual', 'round_robin', 'director')),
    group_max_speakers INTEGER NOT NULL DEFAULT 3 CHECK (group_max_speakers BETWEEN 1 AND 8),
    group_context_mode TEXT NOT NULL DEFAULT 'private' CHECK (group_context_mode IN ('private', 'shared', 'swap')),
    group_sheet_updates INTEGER NOT NULL DEFAULT 0 CHECK (group_sheet_updates IN (0, 1)),
    -- Which group this conversation belongs to: the id of the conversation the
    -- family descends from. NULL means "I am that root", so a plain group needs
    -- no write here and only forks carry a value. ON DELETE SET NULL is the
    -- floor, not the plan -- delete_conversation() promotes a surviving child
    -- to root first, so a family outlives the conversation it started as.
    group_root_id TEXT DEFAULT NULL REFERENCES conversations(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_group_root ON conversations(group_root_id);

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

CREATE TABLE IF NOT EXISTS group_members (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    speaker_key TEXT NOT NULL,
    character_card_id TEXT DEFAULT NULL,
    display_name TEXT NOT NULL,
    public_profile_override TEXT DEFAULT NULL,
    card_sheet_override TEXT DEFAULT NULL,
    member_kind TEXT NOT NULL DEFAULT 'character' CHECK (member_kind IN ('character', 'narrator')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    muted INTEGER NOT NULL DEFAULT 0 CHECK (muted IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    workflow_state TEXT DEFAULT NULL,
    UNIQUE(conversation_id, speaker_key)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_group_member_active_card
ON group_members(conversation_id, character_card_id)
WHERE active = 1 AND character_card_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    turn_index INTEGER NOT NULL,
    parent_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    progressive_fields TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    workflow_state TEXT DEFAULT NULL,
    speaker_member_id TEXT DEFAULT NULL REFERENCES group_members(id) ON DELETE SET NULL,
    beat_id TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_beat ON messages(conversation_id, beat_id);
CREATE INDEX IF NOT EXISTS idx_messages_speaker ON messages(speaker_member_id);

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
    direction_note_timing TEXT NOT NULL DEFAULT 'post_turn'
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
    feedback TEXT NOT NULL DEFAULT '{}'
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
    reasoning_effort_value TEXT NOT NULL DEFAULT '',
    extra_headers TEXT NOT NULL DEFAULT '',
    extra_body TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS worlds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    dynamic_enabled INTEGER NOT NULL DEFAULT 0,
    content_revision INTEGER NOT NULL DEFAULT 0,
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
    at_depth INTEGER NOT NULL DEFAULT 0,
    use_regex INTEGER NOT NULL DEFAULT 0,
    selective INTEGER NOT NULL DEFAULT 0,
    secondary_keys TEXT NOT NULL DEFAULT '[]',
    priority INTEGER NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    entry_layer TEXT NOT NULL DEFAULT 'authored' CHECK (entry_layer IN ('authored', 'dynamic')),
    entry_revision INTEGER NOT NULL DEFAULT 0,
    overlay_action TEXT NOT NULL DEFAULT '' CHECK (overlay_action IN ('', 'add', 'replace', 'suppress')),
    supersedes_entry_id INTEGER DEFAULT NULL REFERENCES lorebook_entries(id) ON DELETE SET NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lorebook_overlay ON lorebook_entries(world_id, entry_layer, archived);

CREATE TABLE IF NOT EXISTS world_changesets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'applied', 'rejected', 'stale', 'superseded', 'reverted')),
    base_revision INTEGER NOT NULL DEFAULT 0,
    applied_revision INTEGER DEFAULT NULL,
    source_user_message_id INTEGER DEFAULT NULL REFERENCES messages(id) ON DELETE SET NULL,
    source_assistant_message_id INTEGER DEFAULT NULL REFERENCES messages(id) ON DELETE SET NULL,
    source_conversation_id TEXT DEFAULT NULL REFERENCES conversations(id) ON DELETE SET NULL,
    source_character_label TEXT NOT NULL DEFAULT '',
    source_conversation_label TEXT NOT NULL DEFAULT '',
    origin TEXT NOT NULL DEFAULT 'agent'
        CHECK (origin IN ('agent', 'undo', 'reset', 're_evaluate', 'manual')),
    summary TEXT NOT NULL DEFAULT '',
    operations TEXT NOT NULL DEFAULT '[]',
    before_entries TEXT NOT NULL DEFAULT '[]',
    after_entries TEXT NOT NULL DEFAULT '[]',
    reverts_changeset_id INTEGER DEFAULT NULL REFERENCES world_changesets(id) ON DELETE SET NULL,
    supersedes_changeset_id INTEGER DEFAULT NULL REFERENCES world_changesets(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT DEFAULT NULL,
    applied_at TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_changeset_world_status ON world_changesets(world_id, status);
CREATE INDEX IF NOT EXISTS idx_changeset_source_asst ON world_changesets(source_assistant_message_id);

CREATE TABLE IF NOT EXISTS member_sheet_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES group_members(id) ON DELETE CASCADE,
    beat_id TEXT NOT NULL DEFAULT '',
    base_sheet TEXT NOT NULL DEFAULT '',
    proposed_sheet TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'rejected', 'stale')),
    created_at TEXT NOT NULL,
    decided_at TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_sheet_proposal_conv_status ON member_sheet_proposals(conversation_id, status);

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
