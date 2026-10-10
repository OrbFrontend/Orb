from __future__ import annotations

import re

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    shared_system_prompt TEXT NOT NULL DEFAULT '',
    -- Read only while no Writer model config is active; the active config's system_prompt replaces it otherwise.
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
    show_chat_avatars INTEGER NOT NULL DEFAULT 0,
    inspector_inline INTEGER NOT NULL DEFAULT 0,
    editor_audit_toggles TEXT NOT NULL DEFAULT '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,"contrastive_negation":true,"phrase_repetition":true,"structural_repetition":true,"anti_echo":true,"negated_narration":false}',
    document_audit_enabled INTEGER NOT NULL DEFAULT 1,
    document_audit_autopatch INTEGER NOT NULL DEFAULT 0,
    document_audit_toggles TEXT NOT NULL DEFAULT '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,"contrastive_negation":true}',
    hide_streaming_until_baked INTEGER NOT NULL DEFAULT 0,
    expression_rendering TEXT NOT NULL DEFAULT 'classic',
    prevent_prompt_overrides INTEGER NOT NULL DEFAULT 0,
    agent_same_as_writer INTEGER NOT NULL DEFAULT 1,
    agent_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL,
    agent_shared_system_prompt TEXT NOT NULL DEFAULT '',
    director_individual_fragments INTEGER NOT NULL DEFAULT 0,
    inspector_open_states TEXT NOT NULL DEFAULT '{"reasoning":true,"tool_calls":false,"injection_block":false,"context_size":true}',
    workflow_config TEXT NOT NULL DEFAULT '{}',
    workflows_globally_enabled INTEGER NOT NULL DEFAULT 1,
    workflow_enabled TEXT NOT NULL DEFAULT '{}',
    local_ml_enabled TEXT NOT NULL DEFAULT '{}',
    local_ml_config TEXT NOT NULL DEFAULT '{}',
    card_source_auth TEXT NOT NULL DEFAULT '{}',
    attachment_cache_budget_bytes INTEGER NOT NULL DEFAULT 524288000,
    attachment_access_counter INTEGER NOT NULL DEFAULT 0,
    generated_chars INTEGER DEFAULT NULL,
    -- The Judge has a dedicated endpoint and model; its route is derived from the URL.
    decision_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL,
    decision_model TEXT NOT NULL DEFAULT 'typesafe/jev-1.13'
);

CREATE TABLE IF NOT EXISTS mood_fragments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    prompt_text TEXT NOT NULL,
    negative_prompt TEXT NOT NULL DEFAULT '',
    cooldown_turns INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_conversations_active_leaf ON conversations(active_leaf_id);

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
    avatar_mime TEXT DEFAULT NULL,
    source_format TEXT NOT NULL DEFAULT 'manual',
    world_id TEXT DEFAULT NULL REFERENCES worlds(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    workflow_state TEXT DEFAULT NULL,
    persona_lock_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL,
    extensions TEXT DEFAULT NULL,
    auto_tag_vocab_hash TEXT NOT NULL DEFAULT '',
    auto_tag_card_updated_at TEXT NOT NULL DEFAULT '',
    -- The duplicate finder's one cached signal: a 64-bit dHash of the decoded
    -- avatar as 16 hex chars, '' when the card has no avatar or its bytes would
    -- not decode. Avatar decoding is ~8.8 ms per card against 0.26s for every
    -- text signal in a 2000-card library combined, so the text side is
    -- recomputed on every scan and only this is stored. The stamp is
    -- f"{DEDUPE_REVISION}:{updated_at}"; a mismatch means re-hash.
    avatar_dhash TEXT NOT NULL DEFAULT '',
    avatar_dhash_stamp TEXT NOT NULL DEFAULT '',
    -- Last on purpose, and it must stay last. An avatar is hundreds of KB of
    -- base64 spilling across overflow pages, and SQLite reaches any column
    -- stored after it only by walking that chain: with the avatar mid-row the
    -- library list read ~370 MB to show 400 names (50 ms warm, 2.4 ms with it
    -- last). A new column goes above this line; an upgrading install's ALTER
    -- TABLE ADD COLUMN lands after it instead, which is correct but slow for
    -- that column until a rebuild migration (see 0073) restores the order.
    avatar_b64 TEXT DEFAULT NULL
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
    writer_draft TEXT DEFAULT NULL,
    turn_index INTEGER NOT NULL,
    parent_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    fragment_cooldowns TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    workflow_state TEXT DEFAULT NULL,
    speaker_member_id TEXT DEFAULT NULL REFERENCES group_members(id) ON DELETE SET NULL,
    exchange_id TEXT DEFAULT NULL,
    -- Versioned evaluations for this reply, used as its replay record.
    decision_evaluations TEXT NOT NULL DEFAULT '{}',
    -- Decision cooldowns count completed exchanges; Director cooldowns count firings.
    decision_cooldowns TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_messages_exchange ON messages(conversation_id, exchange_id);
CREATE INDEX IF NOT EXISTS idx_messages_speaker ON messages(speaker_member_id);
CREATE INDEX IF NOT EXISTS idx_messages_parent ON messages(parent_id);

CREATE TABLE IF NOT EXISTS director_state (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    active_moods TEXT NOT NULL DEFAULT '[]',
    keywords TEXT NOT NULL DEFAULT '[]',
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
    cooldown_turns INTEGER NOT NULL DEFAULT 0,
    -- State-only settings; NULL for other fragment types.
    state_mode TEXT DEFAULT NULL CHECK (state_mode IS NULL OR state_mode IN ('value', 'entries')),
    state_update TEXT DEFAULT NULL CHECK (state_update IS NULL OR state_update IN ('after_reply', 'before_writer', 'manual')),
    state_inject TEXT DEFAULT NULL CHECK (state_inject IS NULL OR state_inject IN ('off', 'director', 'writer', 'both')),
    -- Decision-only fields; NULL for other fragment types and validated together.
    decision_type TEXT DEFAULT NULL,
    decision_placement TEXT DEFAULT NULL,
    decision_inject TEXT DEFAULT NULL CHECK (decision_inject IS NULL OR decision_inject IN ('director', 'writer', 'both')),
    decision_state_template TEXT DEFAULT NULL,
    decision_instructions TEXT DEFAULT NULL,
    decision_criteria TEXT DEFAULT NULL,
    decision_outputs TEXT DEFAULT NULL,
    decision_resolution TEXT DEFAULT NULL,
    decision_threshold REAL DEFAULT NULL,
    decision_confidence_floor REAL DEFAULT NULL,
    -- Post-processing only: the Judge question that must answer yes for the fragment to run; '' always runs.
    post_processing_gate TEXT NOT NULL DEFAULT '',
    -- Post-processing only: how many previous replies the gate shows the Judge before the draft.
    post_processing_gate_replies INTEGER NOT NULL DEFAULT 0
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
    -- State operations the turn rejected and user corrections it could not carry.
    state_report TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_conversation_logs_message ON conversation_logs(message_id);

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
    avatar_b64 TEXT DEFAULT NULL,
    avatar_mime TEXT DEFAULT NULL,
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

CREATE INDEX IF NOT EXISTS idx_user_attachments_message ON user_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_workflow_attachments_message ON workflow_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_workflow_attachments_parent ON workflow_attachments(parent_attachment_id);
CREATE INDEX IF NOT EXISTS idx_workflow_attachments_active_sibling ON workflow_attachments(active_sibling_id);

CREATE TABLE IF NOT EXISTS endpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    api_key TEXT NOT NULL DEFAULT '',
    active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL,
    agent_active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL,
    completion_mode TEXT NOT NULL DEFAULT 'chat' CHECK (completion_mode IN ('chat', 'text')),
    proxy TEXT NOT NULL DEFAULT '',
    -- 'chat' endpoints serve Writer/Agent; 'judge' endpoints serve decision fragments.
    -- Credentials and proxy share a table, but the lanes have separate endpoint lists.
    kind TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'judge'))
);

CREATE TABLE IF NOT EXISTS model_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    endpoint_id INTEGER NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    model_name TEXT NOT NULL,
    system_prompt TEXT NOT NULL DEFAULT '',
    -- NULL means omit the field from provider requests and use its default.
    temperature REAL DEFAULT 0.8,
    min_p REAL DEFAULT 0.0,
    top_k INTEGER DEFAULT 40,
    top_p REAL DEFAULT 0.95,
    repetition_penalty REAL DEFAULT 1.0,
    max_tokens INTEGER DEFAULT 4096,
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
    is_global INTEGER NOT NULL DEFAULT 0,
    dynamic_enabled INTEGER NOT NULL DEFAULT 0,
    content_revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation_worlds (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    enabled INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, world_id)
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
CREATE INDEX IF NOT EXISTS idx_changeset_source_user ON world_changesets(source_user_message_id);

CREATE TABLE IF NOT EXISTS member_sheet_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    member_id TEXT NOT NULL REFERENCES group_members(id) ON DELETE CASCADE,
    exchange_id TEXT NOT NULL DEFAULT '',
    base_sheet TEXT NOT NULL DEFAULT '',
    proposed_sheet TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'applied', 'rejected', 'stale')),
    created_at TEXT NOT NULL,
    decided_at TEXT DEFAULT NULL
);

CREATE INDEX IF NOT EXISTS idx_sheet_proposal_conv_status ON member_sheet_proposals(conversation_id, status);


-- State-fragment history: explicit entry writes and retirements, anchored to the
-- message whose branch they belong to. Folding a branch's events in active-path
-- order (row id within one anchor) yields its active entries, independent of how
-- the fragment is configured now. fragment_label is denormalized so the state
-- stays readable after its fragment is renamed or deleted.
CREATE TABLE IF NOT EXISTS fragment_state_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    fragment_id TEXT NOT NULL,
    entry_id TEXT NOT NULL,
    op TEXT NOT NULL CHECK (op IN ('add', 'revise', 'retire')),
    text TEXT DEFAULT NULL,
    -- The fragment's mode when the change was made; informational, never folded.
    mode TEXT DEFAULT NULL,
    fragment_label TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK (source IN ('agent', 'user', 'carried')),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_state_event_message ON fragment_state_events(message_id);
CREATE INDEX IF NOT EXISTS idx_state_event_conversation ON fragment_state_events(conversation_id, fragment_id);

-- The subject tagger's reading of an assistant reply: per-category (absent, action, description) probabilities as JSON. A cache:
-- content_hash and version say which text and which model/input it read, so an edited reply or a new model re-tags.
CREATE TABLE IF NOT EXISTS message_subjects (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    content_hash TEXT NOT NULL,
    version TEXT NOT NULL,
    probs TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dataset_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    epoch TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS access_password (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT NOT NULL,
    session_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT 'Untitled',
    content TEXT NOT NULL DEFAULT '',
    generated_spans TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS library_tags (
    name TEXT PRIMARY KEY,
    position INTEGER NOT NULL
);

-- Duplicate pairs the user has said to stop reporting.
--
-- Pairs, not groups: group identity is unstable -- one new import reshapes a
-- group and any group key goes stale -- while a pair is stable forever.
-- Dismissing a group of three writes its three pairs, and a fourth card joining
-- later produces new undismissed pairs that correctly re-flag. card_a < card_b
-- canonically, so a pair has exactly one row.
--
-- hash_a/hash_b are the two cards' body_hash at dismissal time. The dismissal
-- lapses the moment either differs, which is "revisit only after meaningful
-- changes" -- and because body_hash excludes tags and public profiles, an
-- auto-tagging run cannot resurrect a dismissed pair.
--
-- ON DELETE CASCADE is load-bearing: connection.py issues PRAGMA foreign_keys=ON
-- on every connection, so deleting a card reaps its dismissals instead of
-- leaking them. (Contrast conversations.character_card_id, which deliberately
-- has no FK so a dangling id can act as a relink marker.)
CREATE TABLE IF NOT EXISTS duplicate_dismissals (
    card_a TEXT NOT NULL REFERENCES character_cards(id) ON DELETE CASCADE,
    card_b TEXT NOT NULL REFERENCES character_cards(id) ON DELETE CASCADE,
    hash_a TEXT NOT NULL,
    hash_b TEXT NOT NULL,
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (card_a, card_b)
);

-- Phrase Bank suggestions mined from model replies across every chat. Derived:
-- each run replaces the table wholesale, and nothing here reaches the bank until
-- the user accepts a row. ``key`` is the mined key ('n:= a beat'), stable across
-- runs; ``pattern`` is the bank-ready regex exactly as it was scored.
CREATE TABLE IF NOT EXISTS slop_suggestions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    lane TEXT NOT NULL CHECK (lane IN ('new', 'longstanding')),
    label TEXT NOT NULL,
    pattern TEXT NOT NULL,
    stats TEXT NOT NULL DEFAULT '{}',
    fillers TEXT NOT NULL DEFAULT '[]',
    examples TEXT NOT NULL DEFAULT '[]',
    mined_at TEXT NOT NULL
);

-- Suggestion keys the user dismissed; a run never suggests them again. These are
-- user decisions, so they travel with the phrase bank in presets and backups.
CREATE TABLE IF NOT EXISTS slop_dismissals (
    key TEXT PRIMARY KEY,
    pattern TEXT NOT NULL,
    dismissed_at TEXT NOT NULL
);

-- The suggestion miner's one bookkeeping row: when it last ran and why the last
-- run suggested nothing or failed.
CREATE TABLE IF NOT EXISTS slop_mining_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_run_at TEXT,
    last_status TEXT NOT NULL DEFAULT ''
);

"""


def table_create_sql(table: str) -> str:
    """Extract canonical CREATE TABLE DDL from CREATE_TABLES_SQL.

    Balance nested parentheses for REFERENCES/CHECK clauses. Rebuild migrations
    and schema-equivalence checks share this definition.
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
