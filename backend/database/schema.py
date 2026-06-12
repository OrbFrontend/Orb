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
    reasoning_enabled_passes TEXT NOT NULL DEFAULT '{"director":true,"writer":false,"editor":false}',
    active_persona_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL,
    active_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL,
    character_library_view TEXT NOT NULL DEFAULT 'grid',
    character_library_sort TEXT NOT NULL DEFAULT 'time-added',
    show_editor_diff INTEGER NOT NULL DEFAULT 1,
    editor_audit_toggles TEXT NOT NULL DEFAULT '{"banned_phrases":true,"repetitive_openers":true,"repetitive_templates":true,"contrastive_negation":true,"phrase_repetition":true,"structural_repetition":true}',
    hide_streaming_until_baked INTEGER NOT NULL DEFAULT 0,
    prevent_prompt_overrides INTEGER NOT NULL DEFAULT 0,
    agent_same_as_writer INTEGER NOT NULL DEFAULT 1,
    agent_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL,
    agent_shared_system_prompt TEXT NOT NULL DEFAULT '',
    feedback_enabled INTEGER NOT NULL DEFAULT 0,
    inspector_open_states TEXT NOT NULL DEFAULT '{"reasoning":true,"tool_calls":false,"injection_block":false,"context_size":true}',
    workflow_config TEXT NOT NULL DEFAULT '{}',
    attachment_cache_budget_bytes INTEGER NOT NULL DEFAULT 524288000,
    attachment_access_counter INTEGER NOT NULL DEFAULT 0
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
    active_leaf_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    workflow_state TEXT DEFAULT NULL,
    persona_lock_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL
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
    persona_lock_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL
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
    progressive_fields TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS interactive_fragments (
    id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    description TEXT NOT NULL,
    field_type TEXT NOT NULL DEFAULT 'string',
    required BOOLEAN NOT NULL DEFAULT 0,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    injection_label TEXT NOT NULL,
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conversation_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    turn_index INTEGER NOT NULL,
    agent_raw_output TEXT,
    tool_calls TEXT,
    active_moods_after TEXT,
    progressive_fields_after TEXT NOT NULL DEFAULT '{}',
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

-- Required on fresh install by migration 0002, which deletes orphan rows
-- from this table before any migration could create it. Migration
-- 0020_workflows copies surviving rows into user_attachments and
-- drops this table at the end of the chain. No rows persist in a
-- fully-migrated database.
CREATE TABLE IF NOT EXISTS message_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    mime_type TEXT NOT NULL,
    data_b64 TEXT NOT NULL,
    filename TEXT,
    size INTEGER,
    created_at TEXT NOT NULL
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
    agent_active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL
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
    role TEXT NOT NULL DEFAULT 'writer' CHECK (role IN ('writer', 'agent'))
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
    priority INTEGER NOT NULL DEFAULT 100,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
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
