from __future__ import annotations
from typing import List, Optional
import asyncio
import aiosqlite
import sqlite3
import json
import os
from datetime import datetime, timezone

from backend.migrations import run_pending as _run_migrations

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")

SEED_MOOD_FRAGMENTS = [
    {
        "id": "talkative",
        "label": "Talkative",
        "description": "Lean into dialogue and natural speech",
        "prompt_text": (
            "Lean into dialogue. Characters express themselves through speech. "
            "Use dialogue tags sparingly — let the words carry the tone. Include verbal tics, "
            "interruptions, trailing off, and naturalistic speech patterns."
        ),
        "negative_prompt": (
            "Pull back from heavy dialogue. Return to a balanced mix of prose and speech. "
            "Do not prioritize dialogue over action and description."
        ),
    },
    {
        "id": "inner-thoughts",
        "label": "Inner Thoughts",
        "description": "Foreground the character's inner thoughts. Only use when the user is acting strange.",
        "prompt_text": (
            "Foreground the character's inner thoughts. Show the gap between what they think "
            "and what they say. Stream of consciousness is acceptable. The reader should feel "
            "like they're inside the character's head. "
            "The narration should share the character's world view, inject the character's opinion and use their vocabulary in the prose."
        ),
        "negative_prompt": (
            "Stop foregrounding inner thoughts. Return to external action and dialogue. "
            "Keep interiority brief and grounded."
        ),
    },
    {
        "id": "terse",
        "label": "Terse",
        "description": "Short, punchy prose with no filler, like a haiku. Use to conclude a chapter/scene.",
        "prompt_text": (
            "Your mood has NOW shifted — use short, clipped prose. Cut adjectives. "
            "Cut adverbs. Every sentence earns its place or gets deleted. Paragraphs are 1-3 "
            "sentences max. This overrides your previous tendencies toward longer prose."
        ),
        "negative_prompt": (
            "Return to normal prose length. You may use full sentences, adjectives, and longer "
            "paragraphs again. Do not keep clipping sentences artificially."
        ),
    },
    {
        "id": "tense",
        "label": "Tense",
        "description": "Suspenseful, high-stakes atmosphere",
        "prompt_text": (
            "Write with tension and unease. Use silence and pauses. Characters are hyper-aware "
            "of their surroundings. Minimize humor unless dark or nervous. The reader should "
            "feel that something could go wrong at any moment."
        ),
        "negative_prompt": (
            "Release the tension. The scene no longer needs to feel high-stakes or ominous. "
            "Allow the tone to relax and breathe."
        ),
    },
    {
        "id": "sensual",
        "label": "Sensual",
        "description": "NSFW mode - enable if the vibes of the situation call for it",
        "prompt_text": (
            "The narration may describe body parts that are exposed or looked at - visually, or interacted with - tactilely, but sparingly in order to avoid repetition. Take initiative and actively push the scene forward, no need to ask. "
            'The narration can use NSFW, vulgar, uncensored, explicit vocabulary where applicable. Avoid roundabout euphemisms like "heat", "core", etc. The prose is now more brutal, vivid, and direct. '
            "Consider positions and clothing states of the characters - what are they wearing, and where are they in relation to each other? "
            "Focus on staying in character even though the vibes are sensual - characters still have their own boundaries and will behave realistically and warily of strangers."
        ),
        "negative_prompt": (
            "Return to non-explicit writing. Keep all descriptions tasteful and non-graphic. "
            "Do not use vulgar sexual vocabulary or describe exposed body parts explicitly."
        ),
    },
    {
        "id": "defensive",
        "label": "Defensive",
        "description": "Some characters don't take kindly to insults, sexual assault, violence, non-consensual advances, and will react.",
        "prompt_text": (
            "Some characters don't take kindly to insults, sexual assault, violence, non-consensual advances. They will fight back or escape (or at least try to) according to their personalities. React immediately."
        ),
        "negative_prompt": "",
    },
    {
        "id": "grounded",
        "label": "Grounded",
        "description": "The characters are behaving irrationally/illogically (porn logic, too friendly towards strangers, non-sensical power-scaling, etc.), time to reign them in and make them act more realistic.",
        "prompt_text": (
            "The scenario is getting far-fetched and characters are behaving irrationally/illogically. Focus on being realistic and grounded now, the characters should act like how real people act, talk like how real people talk. That means less monologue, more wariness of strangers, balanced power-scaling, etc."
        ),
        "negative_prompt": "",
    },
]

SEED_DIRECTOR_FRAGMENTS = [
    {
        "id": "plot_summary",
        "label": "Plot Summary",
        "description": (
            "A brief and specific summary of what has happened so far in the story. "
            "Call things for what they are, avoid being generic, avoid adjectives. "
            "3 sentences max (e.g. Rob was working on his lake house when his wife called for him to help moving some furniture. "
            "The weather was hot so he took off his shirt. Then the couch fell on his leg, eliciting his pain receptors.)."
        ),
        "field_type": "string",
        "required": True,
        "injection_label": "Plot summary",
        "sort_order": 0,
    },
    {
        "id": "user_intent",
        "label": "User Intent",
        "description": (
            "Hidden/subtle intention of the user based on their latest input — what they want to see. "
            "Be extremely literal and specific (e.g. 'This crosses the line, the user wants to find out what happens when boundaries are crossed', "
            "'The user is being a tsundere', "
            "'The user is confessing his love in a roundabout way', "
            "'The user wants to push the scenario forward already')."
        ),
        "field_type": "string",
        "required": False,
        "injection_label": "User intent",
        "sort_order": 1,
    },
    {
        "id": "keywords",
        "label": "Keywords",
        "description": (
            "List of nouns (keywords) to remind the important subjects in the roleplay so far. "
            "This list shouldn't grow too long (keep under 6 items). Extract from the messages and plot summary. "
            "Ignore obvious things like names of the characters. "
            "Examples: 'ancient Egypt', 'headlock', 'monetary deal', 'language/accent', 'desert night', "
            "'six-sided dice', 'discarded belt'. Avoid generic concepts (e.g. 'anger', 'ruin', etc.)"
        ),
        "field_type": "array",
        "required": True,
        "injection_label": "Keywords",
        "sort_order": 2,
    },
    {
        "id": "next_event",
        "label": "Next Event",
        "description": (
            "What happens immediately next in the story — the next event, action, reveal, or turn of fate "
            "(e.g. 'This act crosses personal boundaries. The character snaps and fights back.', "
            "'The attack tears off a chunk of her clothing. She frantically tries to cover herself', "
            "'Jack can tell she\\'s lying. He calls her out on it because they have been friends forever', "
            "'She pretends not to know what Vodka is to keep up the innocent act', "
            "'He gets bored and shifts focus to something else entirely'). Keep to two short sentences."
        ),
        "field_type": "string",
        "required": True,
        "injection_label": "Next event",
        "sort_order": 3,
    },
    {
        "id": "writing_direction",
        "label": "Writing Direction",
        "description": (
            "How the scene should be written — focus, emphasis, descriptive lens, internal state "
            "(e.g. 'focus on his anxious tics in detail', 'narrate her spiraling thoughts on why it went wrong', "
            "'describe her exposed stomach vividly', 'describe what he sees in the picture', "
            "'emphasize her speech quirks'). Keep to one short sentence. Show don't tell."
        ),
        "field_type": "string",
        "required": True,
        "injection_label": "Narration",
        "sort_order": 4,
    },
    {
        "id": "detected_repetitions",
        "label": "Detected Repetitions",
        "description": (
            "Specific tropes, phrases, subjects, plot points, narrative patterns that are recently overused in the narration "
            "(e.g. 'banal description of eyes', 'mundane narration of internal struggles', 'overuse of murderous rage', "
            "'repeated trope of the user getting away with everything', 'constant narration of his accent without showing it', "
            "'constant focus on the tree'). This list may have up to 8 items."
        ),
        "field_type": "array",
        "required": False,
        "injection_label": "Avoid repeating",
        "sort_order": 5,
    },
]

DEFAULT_ENABLED_TOOLS = {
    "direct_scene": True,
    "rewrite_user_prompt": False,
    "editor_apply_patch": False,
    "editor_rewrite": False,
}

DEFAULT_SETTINGS = {
    "endpoint_url": "http://localhost:5000/v1",
    "api_key": "",
    "model_name": "default",
    "temperature": 0.8,
    "min_p": 0,
    "top_k": 40,
    "top_p": 0.95,
    "repetition_penalty": 1.0,
    "max_tokens": 4096,
    "shared_system_prompt": "You are a creative roleplay partner. Be responsive to the scene's evolving tone.\nCharacters have their own conviction and ideas, they may disagree with each other.\nKeep tenses (past, present) and POV consistent.\nAvoid repetition of word choices and sentence structures.",
    "system_prompt": "",
    "user_name": "User",
    "user_description": "",
    "enable_agent": True,
    "length_guard_max_words": 240,
    "length_guard_max_paragraphs": 4,
    "character_library_view": "grid",
    "character_library_sort": "time-added",
}

SEED_PHRASE_BANK = [
    ["a mix of", "a mixture of"],
    ["dripped with", "dripping with", "drips with"],
    [
        "the air was heavy",
        "the air is heavy",
        "the air was charged",
        "the air is charged",
        "the air was thick",
        "the air is thick",
    ],
    ["tension in the air"],
    ["filling the air", "fills the air", "filled the air"],
    [
        "hang in the air",
        "hung in the air",
        "hangs in the air",
        "hanging in the air",
        "the air between them",
    ],
    ["dangerous voice", "dangerous tone"],
    [
        "voice dropping",
        "voice low",
        "voice dangerous",
        "voice a dangerous",
        "voice a low",
        "voice is a low",
        "voice is a dangerous",
    ],
    ["low hiss", "dangerous hiss"],
    ["barely a whisper", "barely above a whisper", "barely audible"],
    ["voice cracks", "voice cracking", "voice cracked"],
    ["a low, guttural", "a guttural sound"],
    [
        "a predatory smirk",
        "I don't bite",
        "they don't bite",
        "it doesn't bite",
        "predatory glee",
    ],
    [
        "very brave or very stupid",
        "either very brave or very foolish",
        "brave or stupid",
    ],
    ["sending shivers", "sending a shiver"],
    ["a dance of", "a dance between", "dancing with"],
    [
        "eyes narrowing",
        "eyes narrowed",
        "mischievous glint",
        "glint with mischief",
        "gaze sharpen",
        "eyes widen",
        "eyes wide",
    ],
    [
        "eyes never leaving his",
        "eyes never leaving hers",
        "eyes never leave his",
        "eyes never leave hers",
    ],
    ["breath hitches", "breath hitched", "breath hitching", "breath catching"],
    ["ozone"],
    ["purr", "purred", "purrs"],
    ["conspiratorial"],
    ["testament to"],
    ["honeyed", "velvet", "porcelain", "intoxicating"],
    ["like a vice", "like a vise"],
    ["void", "shadowed"],
    ["incredulous"],
    ["predatory", "primal"],
    ["vulnerability", "vulnerable"],
    ["don't you dare stop"],
    ["electric", "electrifying"],
    ["thick and suffocating", "thick, suffocating"],
    ["mind races", "mind racing", "mind raced"],
    ["knuckles whitening", "knuckles whitened", "whitened knuckles"],
    ["stark contrast", "pure, unadulterated"],
]


async def get_db() -> aiosqlite.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript(
            """
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
                reasoning_enabled_passes TEXT NOT NULL DEFAULT '{"director":true,"writer":false,"editor":false}',
                active_persona_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL,
                character_library_view TEXT NOT NULL DEFAULT 'grid',
                character_library_sort TEXT NOT NULL DEFAULT 'time-added'
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
                first_mes TEXT NOT NULL DEFAULT '',
                post_history_instructions TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT,
                active_leaf_id INTEGER REFERENCES messages(id) ON DELETE SET NULL
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
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                turn_index INTEGER NOT NULL,
                swipe_index INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                parent_id INTEGER REFERENCES messages(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS director_state (
                conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
                active_moods TEXT NOT NULL DEFAULT '[]',
                keywords TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS director_fragments (
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
                injection_block TEXT,
                agent_latency_ms INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS phrase_bank (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variants TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_personas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                avatar_color TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                mime_type TEXT NOT NULL,
                data_b64 TEXT NOT NULL,
                filename TEXT,
                size INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                api_key TEXT NOT NULL DEFAULT '',
                active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL
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
                max_tokens INTEGER NOT NULL DEFAULT 4096
            );
        """
        )

        # Migrations for existing DBs
        existing_cols = {
            row[1] for row in await db.execute_fetchall("PRAGMA table_info(settings)")
        }
        if "enable_agent" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN enable_agent INTEGER NOT NULL DEFAULT 1"
            )
        if "length_guard_max_words" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN length_guard_max_words INTEGER NOT NULL DEFAULT 400"
            )
        if "length_guard_max_paragraphs" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN length_guard_max_paragraphs INTEGER NOT NULL DEFAULT 5"
            )
        if "reasoning_enabled_passes" not in existing_cols:
            await db.execute(
                'ALTER TABLE settings ADD COLUMN reasoning_enabled_passes TEXT NOT NULL DEFAULT \'{"director":true,"writer":false,"editor":false}\''
            )

        if "active_persona_id" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN active_persona_id INTEGER REFERENCES user_personas(id) ON DELETE SET NULL"
            )
        if "character_library_view" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN character_library_view TEXT NOT NULL DEFAULT 'grid'"
            )
        if "character_library_sort" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN character_library_sort TEXT NOT NULL DEFAULT 'time-added'"
            )
        if "active_endpoint_id" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN active_endpoint_id INTEGER REFERENCES endpoints(id) ON DELETE SET NULL"
            )
        if "shared_system_prompt" not in existing_cols:
            await db.execute(
                "ALTER TABLE settings ADD COLUMN shared_system_prompt TEXT NOT NULL DEFAULT ''"
            )
        endpoint_cols = {
            row[1] for row in await db.execute_fetchall("PRAGMA table_info(endpoints)")
        }
        if "active_model_config_id" not in endpoint_cols:
            await db.execute(
                "ALTER TABLE endpoints ADD COLUMN active_model_config_id INTEGER REFERENCES model_configs(id) ON DELETE SET NULL"
            )

        # Migration for director_state keywords column
        director_cols = {
            row[1]
            for row in await db.execute_fetchall("PRAGMA table_info(director_state)")
        }
        if "keywords" not in director_cols:
            await db.execute(
                "ALTER TABLE director_state ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]'"
            )

        # No migration needed for UUID character IDs: character_cards.id and
        # conversations.character_card_id are already TEXT columns that accept any
        # string. Existing slug-based IDs remain valid; only new characters get UUIDs.

        # Migration for mood fragments enabled column
        fragment_cols = {
            row[1]
            for row in await db.execute_fetchall("PRAGMA table_info(mood_fragments)")
        }
        if "enabled" not in fragment_cols:
            await db.execute(
                "ALTER TABLE mood_fragments ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1"
            )

        # Seed settings if empty
        row = await db.execute_fetchall("SELECT COUNT(*) as c FROM settings")
        if row[0]["c"] == 0:
            s = DEFAULT_SETTINGS
            await db.execute(
                "INSERT INTO settings (id, endpoint_url, model_name, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, shared_system_prompt, system_prompt, enabled_tools) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    s["endpoint_url"],
                    s["model_name"],
                    s["temperature"],
                    s["min_p"],
                    s["top_k"],
                    s["top_p"],
                    s["repetition_penalty"],
                    s["max_tokens"],
                    s["shared_system_prompt"],
                    s["system_prompt"],
                    json.dumps(DEFAULT_ENABLED_TOOLS),
                ),
            )

        # Seed endpoints from existing settings if endpoints table is empty
        ep_row = await db.execute_fetchall("SELECT COUNT(*) as c FROM endpoints")
        if ep_row[0]["c"] == 0:
            s_rows = await db.execute_fetchall("SELECT * FROM settings WHERE id = 1")
            if s_rows:
                s = dict(s_rows[0])
                cur = await db.execute(
                    "INSERT INTO endpoints (url, api_key) VALUES (?, ?)",
                    (
                        s.get("endpoint_url", "http://localhost:5000/v1"),
                        s.get("api_key", ""),
                    ),
                )
                endpoint_id = cur.lastrowid
                cur2 = await db.execute(
                    "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        endpoint_id,
                        s.get("model_name", "default"),
                        "",  # Model-specific system_prompt starts empty
                        s.get("temperature", 0.8),
                        s.get("min_p", 0.0),
                        s.get("top_k", 40),
                        s.get("top_p", 0.95),
                        s.get("repetition_penalty", 1.0),
                        s.get("max_tokens", 4096),
                    ),
                )
                model_config_id = cur2.lastrowid
                await db.execute(
                    "UPDATE endpoints SET active_model_config_id = ? WHERE id = ?",
                    (model_config_id, endpoint_id),
                )
                await db.execute(
                    "UPDATE settings SET active_endpoint_id = ? WHERE id = 1",
                    (endpoint_id,),
                )

        # Seed mood fragments if empty
        row = await db.execute_fetchall("SELECT COUNT(*) as c FROM mood_fragments")
        if row[0]["c"] == 0:
            for f in SEED_MOOD_FRAGMENTS:
                await db.execute(
                    "INSERT INTO mood_fragments (id, label, description, prompt_text, negative_prompt) VALUES (?, ?, ?, ?, ?)",
                    (
                        f["id"],
                        f["label"],
                        f["description"],
                        f["prompt_text"],
                        f["negative_prompt"],
                    ),
                )

        # Seed director_fragments if empty
        row = await db.execute_fetchall("SELECT COUNT(*) as c FROM director_fragments")
        if row[0]["c"] == 0:
            for df in SEED_DIRECTOR_FRAGMENTS:
                await db.execute(
                    "INSERT INTO director_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                    (
                        df["id"],
                        df["label"],
                        df["description"],
                        df["field_type"],
                        1 if df["required"] else 0,
                        df["injection_label"],
                        df["sort_order"],
                    ),
                )

        # Seed phrase_bank if empty
        row = await db.execute_fetchall("SELECT COUNT(*) as c FROM phrase_bank")
        if row[0]["c"] == 0:
            for variants in SEED_PHRASE_BANK:
                await db.execute(
                    "INSERT INTO phrase_bank (variants) VALUES (?)",
                    (json.dumps(variants),),
                )

        await db.commit()

    finally:
        await db.close()

    await asyncio.to_thread(_run_migrations, DB_PATH)


# --- Settings ---


async def get_settings() -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM settings WHERE id = 1")
        if not rows:
            return DEFAULT_SETTINGS
        s = dict(rows[0])
        s["enabled_tools"] = json.loads(s.get("enabled_tools") or "{}")
        s["reasoning_enabled_passes"] = json.loads(
            s.get("reasoning_enabled_passes")
            or '{"director":true,"writer":false,"editor":false}'
        )
        # Overlay endpoint_url, api_key, model_name, and hyperparameters from the
        # active endpoint's active model config so callers always get live values
        # rather than the stale flat columns.
        active_ep_id = s.get("active_endpoint_id")
        if active_ep_id:
            ep_rows = await db.execute_fetchall(
                "SELECT id, url, api_key, active_model_config_id FROM endpoints WHERE id = ?",
                (active_ep_id,),
            )
            if ep_rows:
                ep = dict(ep_rows[0])
                mc_id = ep.get("active_model_config_id")
                if mc_id:
                    mc_rows = await db.execute_fetchall(
                        """SELECT mc.*, e.url AS endpoint_url, e.api_key
                           FROM model_configs mc
                           JOIN endpoints e ON mc.endpoint_id = e.id
                           WHERE mc.id = ?""",
                        (mc_id,),
                    )
                    if mc_rows:
                        mc = dict(mc_rows[0])
                        s["endpoint_url"] = mc["endpoint_url"]
                        s["api_key"] = mc.get("api_key", "")
                        s["model_name"] = mc["model_name"]
                        for field in (
                            "temperature",
                            "min_p",
                            "top_k",
                            "top_p",
                            "repetition_penalty",
                            "max_tokens",
                        ):
                            if mc.get(field) is not None:
                                s[field] = mc[field]
                        if mc.get("system_prompt") is not None:
                            s["system_prompt"] = mc["system_prompt"]
        return s
    finally:
        await db.close()


async def update_settings(data: dict) -> dict:
    db = await get_db()
    try:
        allowed = [
            "endpoint_url",
            "api_key",
            "model_name",
            "temperature",
            "min_p",
            "top_k",
            "top_p",
            "repetition_penalty",
            "max_tokens",
            "shared_system_prompt",
            "system_prompt",
            "user_name",
            "user_description",
            "enabled_tools",
            "enable_agent",
            "length_guard_max_words",
            "length_guard_max_paragraphs",
            "reasoning_enabled_passes",
            "active_persona_id",
            "character_library_view",
            "character_library_sort",
            "active_endpoint_id",
        ]
        json_fields = {"enabled_tools", "reasoning_enabled_passes"}
        sets = []
        vals = []

        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(json.dumps(data[k]) if k in json_fields else data[k])
        if sets:
            await db.execute(
                f"UPDATE settings SET {', '.join(sets)} WHERE id = 1",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_settings()
    finally:
        await db.close()


# --- Endpoints ---


async def get_endpoints() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, url, api_key, active_model_config_id FROM endpoints ORDER BY id ASC"
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_endpoint(endpoint_id: int) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, url, api_key, active_model_config_id FROM endpoints WHERE id = ?",
            (endpoint_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_endpoint(url: str, api_key: str = "") -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO endpoints (url, api_key) VALUES (?, ?)", (url, api_key)
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT id, url FROM endpoints WHERE id = ?", (cur.lastrowid,)
        )
        return dict(rows[0])
    finally:
        await db.close()


async def update_endpoint(endpoint_id: int, data: dict) -> dict | None:
    db = await get_db()
    try:
        allowed = ["url", "api_key", "active_model_config_id"]
        sets, vals = [], []
        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(data[k])
        if sets:
            vals.append(endpoint_id)
            await db.execute(
                f"UPDATE endpoints SET {', '.join(sets)} WHERE id = ?",  # nosec B608
                vals,
            )
            await db.commit()
        rows = await db.execute_fetchall(
            "SELECT id, url, api_key, active_model_config_id FROM endpoints WHERE id = ?",
            (endpoint_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def delete_endpoint(endpoint_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM endpoints WHERE id = ?", (endpoint_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- Model Configs ---


async def get_model_configs(endpoint_id: int) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM model_configs WHERE endpoint_id = ? ORDER BY id ASC",
            (endpoint_id,),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def create_model_config(endpoint_id: int, data: dict) -> dict:
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                endpoint_id,
                data.get("model_name", "default"),
                data.get("system_prompt", ""),
                data.get("temperature", 0.8),
                data.get("min_p", 0.0),
                data.get("top_k", 40),
                data.get("top_p", 0.95),
                data.get("repetition_penalty", 1.0),
                data.get("max_tokens", 4096),
            ),
        )
        await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM model_configs WHERE id = ?", (cur.lastrowid,)
        )
        return dict(rows[0])
    finally:
        await db.close()


async def update_model_config(config_id: int, data: dict) -> dict | None:
    db = await get_db()
    try:
        allowed = [
            "model_name",
            "system_prompt",
            "temperature",
            "min_p",
            "top_k",
            "top_p",
            "repetition_penalty",
            "max_tokens",
        ]
        sets, vals = [], []
        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(data[k])
        if sets:
            vals.append(config_id)
            await db.execute(
                f"UPDATE model_configs SET {', '.join(sets)} WHERE id = ?",  # nosec B608
                vals,
            )
            await db.commit()
        rows = await db.execute_fetchall(
            "SELECT * FROM model_configs WHERE id = ?", (config_id,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def delete_model_config(config_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM model_configs WHERE id = ?", (config_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- Mood Fragments ---


async def get_mood_fragments() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM mood_fragments ORDER BY label ASC"
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_mood_fragment(fid: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM mood_fragments WHERE id = ?", (fid,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_mood_fragment(data: dict) -> dict:
    db = await get_db()
    try:
        enabled = data.get("enabled", 1)
        await db.execute(
            "INSERT INTO mood_fragments (id, label, description, prompt_text, negative_prompt, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                data["label"],
                data["description"],
                data["prompt_text"],
                data.get("negative_prompt", ""),
                enabled,
            ),
        )
        await db.commit()
        return await get_mood_fragment(data["id"])
    finally:
        await db.close()


async def update_mood_fragment(fid: str, data: dict) -> dict | None:
    db = await get_db()
    try:
        allowed = ["label", "description", "prompt_text", "negative_prompt", "enabled"]
        sets = []
        vals = []
        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(data[k])
        if sets:
            vals.append(fid)
            await db.execute(
                f"UPDATE mood_fragments SET {', '.join(sets)} WHERE id = ?",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_mood_fragment(fid)
    finally:
        await db.close()


async def delete_mood_fragment(fid: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM mood_fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- Director Fragments ---


async def get_director_fragments() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM director_fragments ORDER BY sort_order ASC, label ASC"
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_director_fragment(fid: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM director_fragments WHERE id = ?", (fid,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_director_fragment(data: dict) -> dict | None:
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO director_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                data["id"],
                data["label"],
                data["description"],
                data.get("field_type", "string"),
                1 if data.get("required", False) else 0,
                1 if data.get("enabled", True) else 0,
                data["injection_label"],
                data.get("sort_order", 0),
            ),
        )
        await db.commit()
        return await get_director_fragment(data["id"])
    finally:
        await db.close()


async def update_director_fragment(fid: str, data: dict) -> dict | None:
    db = await get_db()
    try:
        allowed = [
            "label",
            "description",
            "field_type",
            "required",
            "enabled",
            "injection_label",
            "sort_order",
        ]
        sets = []
        vals = []
        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(data[k])
        if sets:
            vals.append(fid)
            await db.execute(
                f"UPDATE director_fragments SET {', '.join(sets)} WHERE id = ?",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_director_fragment(fid)
    finally:
        await db.close()


async def delete_director_fragment(fid: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM director_fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- Conversations ---


async def list_conversations() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """
            SELECT c.*,
                   (SELECT m.content FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS last_message_preview
            FROM conversations c
            ORDER BY COALESCE(c.updated_at, c.created_at) DESC
        """
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_conversation(cid: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM conversations WHERE id = ?", (cid,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_conversation(
    cid: str,
    title: str,
    char_name: str,
    char_scenario: str,
    first_mes: str = "",
    post_history_instructions: str = "",
    character_card_id: str | None = None,
) -> dict:
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO conversations
               (id, title, character_card_id, character_name, character_scenario,
                first_mes, post_history_instructions, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cid,
                title,
                character_card_id,
                char_name,
                char_scenario,
                first_mes,
                post_history_instructions,
                now,
                now,
            ),
        )
        await db.execute(
            "INSERT INTO director_state (conversation_id, active_moods, keywords) VALUES (?, '[]', '[]')",
            (cid,),
        )
        await db.commit()
        return await get_conversation(cid)
    finally:
        await db.close()


async def delete_conversation(cid: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM conversations WHERE id = ?", (cid,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def touch_conversation(cid: str) -> bool:
    """Update conversation's updated_at to current time."""
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, cid)
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- Messages ---


async def _get_path_to_leaf(cid: str, leaf_id: int) -> list[dict]:
    """Walk parent_id chain from leaf to root, return ordered root→leaf."""
    db = await get_db()
    try:
        path = []
        current_id = leaf_id
        while current_id is not None:
            rows = await db.execute_fetchall(
                "SELECT * FROM messages WHERE id = ? AND conversation_id = ?",
                (current_id, cid),
            )
            if not rows:
                break
            msg = dict(rows[0])
            path.append(msg)
            current_id = msg.get("parent_id")
        path.reverse()
        return path
    finally:
        await db.close()


async def get_messages(cid: str) -> list[dict]:
    """Get active path messages (root→leaf) for LLM prompt construction."""
    conv = await get_conversation(cid)
    if not conv:
        return []
    leaf_id = conv.get("active_leaf_id")
    if not leaf_id:
        return []
    messages = await _get_path_to_leaf(cid, leaf_id)
    # Add attachments to each message
    for msg in messages:
        msg["attachments"] = await get_attachments_for_message(msg["id"])
    return messages


async def get_messages_with_branch_info(cid: str) -> list[dict]:
    """Get active path messages with branch navigation metadata for the frontend."""
    messages = await get_messages(cid)
    if not messages:
        return []
    db = await get_db()
    try:
        for msg in messages:
            parent_id = msg.get("parent_id")
            if parent_id is None:
                sibling_rows = await db.execute_fetchall(
                    "SELECT id FROM messages WHERE conversation_id = ? AND parent_id IS NULL ORDER BY id ASC",
                    (cid,),
                )
            else:
                sibling_rows = await db.execute_fetchall(
                    "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? ORDER BY id ASC",
                    (cid, parent_id),
                )
            sibling_ids = [r["id"] for r in sibling_rows]
            idx = sibling_ids.index(msg["id"]) if msg["id"] in sibling_ids else 0
            msg["branch_count"] = len(sibling_ids)
            msg["branch_index"] = idx
            msg["prev_branch_id"] = sibling_ids[idx - 1] if idx > 0 else None
            msg["next_branch_id"] = (
                sibling_ids[idx + 1] if idx < len(sibling_ids) - 1 else None
            )
        # Add attachments
        for msg in messages:
            msg["attachments"] = await get_attachments_for_message(msg["id"])
        return messages
    finally:
        await db.close()


async def add_message(
    cid: str,
    role: str,
    content: str,
    turn_index: int,
    swipe_index: int = 0,
    parent_id: int | None = None,
    attachments: Optional[List[dict]] = None,
) -> int:
    """Add a message. Returns the new message id.
    If attachments is provided, each dict must have keys:
    - mime_type (str)
    - data_b64 (str)
    - filename (optional str)
    - size (optional int)
    """
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        try:
            cur = await db.execute(
                "INSERT INTO messages (conversation_id, role, content, turn_index, swipe_index, is_active, parent_id, created_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (cid, role, content, turn_index, swipe_index, parent_id, now),
            )
        except sqlite3.IntegrityError as e:
            raise ValueError(
                f"Foreign key constraint failed for conversation={cid}, parent={parent_id}: {e}"
            ) from e
        message_id = cur.lastrowid
        if attachments:
            for att in attachments:
                await db.execute(
                    "INSERT INTO message_attachments (message_id, mime_type, data_b64, filename, size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        att["mime_type"],
                        att["data_b64"],
                        att.get("filename"),
                        att.get("size"),
                        now,
                    ),
                )
        await db.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, cid)
        )
        await db.commit()
        return message_id
    finally:
        await db.close()


async def get_attachments_for_message(message_id: int) -> List[dict]:
    """Retrieve all attachments for a message."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, mime_type, data_b64, filename, size, created_at FROM message_attachments WHERE message_id = ? ORDER BY id",
            (message_id,),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def update_message_content(msg_id: int, content: str) -> None:
    """Update the content of an existing message."""
    db = await get_db()
    try:
        await db.execute(
            "UPDATE messages SET content = ? WHERE id = ?", (content, msg_id)
        )
        await db.commit()
    finally:
        await db.close()


async def get_message_by_id(msg_id: int) -> dict | None:
    """Fetch a single message by its primary key."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def set_active_leaf(cid: str, leaf_id: int | None):
    """Update the active_leaf_id for a conversation."""
    db = await get_db()
    try:
        if leaf_id is not None:
            rows = await db.execute_fetchall(
                "SELECT id FROM messages WHERE id = ? AND conversation_id = ?",
                (leaf_id, cid),
            )
            if not rows:
                raise ValueError(
                    f"Message {leaf_id} does not exist in conversation {cid}"
                )
        await db.execute(
            "UPDATE conversations SET active_leaf_id = ? WHERE id = ?", (leaf_id, cid)
        )
        await db.commit()
    finally:
        await db.close()


async def get_deepest_descendant(cid: str, message_id: int) -> int:
    """Return the deepest descendant of message_id (most recently added child chain)."""
    db = await get_db()
    try:
        current_id = message_id
        while True:
            rows = await db.execute_fetchall(
                "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? ORDER BY id DESC LIMIT 1",
                (cid, current_id),
            )
            if not rows:
                break
            current_id = rows[0]["id"]
        return current_id
    finally:
        await db.close()


async def switch_to_branch(cid: str, message_id: int) -> bool:
    """Set active leaf to the deepest descendant of message_id. Returns False if not found."""
    msg = await get_message_by_id(message_id)
    if not msg or msg["conversation_id"] != cid:
        return False
    leaf_id = await get_deepest_descendant(cid, message_id)
    await set_active_leaf(cid, leaf_id)
    return True


async def create_swipe(cid: str, turn_index: int, content: str) -> dict:
    """Create a new swipe at a given turn_index. Deactivates old swipes, activates the new one."""
    db = await get_db()
    try:
        # Get the role and next swipe_index
        rows = await db.execute_fetchall(
            "SELECT role, MAX(swipe_index) as max_si FROM messages WHERE conversation_id = ? AND turn_index = ?",
            (cid, turn_index),
        )
        if not rows or rows[0]["role"] is None:
            raise ValueError(f"No messages at turn_index {turn_index}")

        role = rows[0]["role"]
        new_si = (rows[0]["max_si"] or 0) + 1

        # Deactivate all swipes at this turn
        await db.execute(
            "UPDATE messages SET is_active = 0 WHERE conversation_id = ? AND turn_index = ?",
            (cid, turn_index),
        )

        # Insert new active swipe
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO messages (conversation_id, role, content, turn_index, swipe_index, is_active, created_at) VALUES (?, ?, ?, ?, ?, 1, ?)",
            (cid, role, content, turn_index, new_si, now),
        )
        await db.commit()

        return {"turn_index": turn_index, "swipe_index": new_si, "role": role}
    finally:
        await db.close()


async def switch_swipe(cid: str, turn_index: int, target_swipe_index: int) -> bool:
    """Switch the active swipe at a given turn_index."""
    db = await get_db()
    try:
        # Verify the target exists
        rows = await db.execute_fetchall(
            "SELECT id FROM messages WHERE conversation_id = ? AND turn_index = ? AND swipe_index = ?",
            (cid, turn_index, target_swipe_index),
        )
        if not rows:
            return False

        # Deactivate all, activate target
        await db.execute(
            "UPDATE messages SET is_active = 0 WHERE conversation_id = ? AND turn_index = ?",
            (cid, turn_index),
        )
        await db.execute(
            "UPDATE messages SET is_active = 1 WHERE conversation_id = ? AND turn_index = ? AND swipe_index = ?",
            (cid, turn_index, target_swipe_index),
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def truncate_after_turn(cid: str, turn_index: int):
    """Delete all messages with turn_index > the given value."""
    db = await get_db()
    try:
        await db.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND turn_index > ?",
            (cid, turn_index),
        )
        # Also delete conversation logs after this turn
        await db.execute(
            "DELETE FROM conversation_logs WHERE conversation_id = ? AND turn_index > ?",
            (cid, turn_index),
        )
        await db.commit()
    finally:
        await db.close()


async def get_next_turn_index(cid: str) -> int:
    """Get the next turn_index based on the active leaf's position."""
    conv = await get_conversation(cid)
    if not conv:
        return 0
    leaf_id = conv.get("active_leaf_id")
    if not leaf_id:
        return 0
    msg = await get_message_by_id(leaf_id)
    return (msg["turn_index"] + 1) if msg else 0


# --- Director State ---


async def get_director_state(cid: str) -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM director_state WHERE conversation_id = ?", (cid,)
        )
        if rows:
            r = dict(rows[0])
            r["active_moods"] = json.loads(r["active_moods"])
            # Handle keywords column (may be missing in older DBs)
            if "keywords" in r and r["keywords"]:
                r["keywords"] = json.loads(r["keywords"])
            else:
                r["keywords"] = []
            return r
        return {"conversation_id": cid, "active_moods": [], "keywords": []}
    finally:
        await db.close()


async def update_director_state(
    cid: str, active_moods: list, keywords: list | None = None
):
    db = await get_db()
    try:
        if keywords is not None:
            await db.execute(
                "UPDATE director_state SET active_moods = ?, keywords = ? WHERE conversation_id = ?",
                (json.dumps(active_moods), json.dumps(keywords), cid),
            )
        else:
            await db.execute(
                "UPDATE director_state SET active_moods = ? WHERE conversation_id = ?",
                (json.dumps(active_moods), cid),
            )
        await db.commit()
    finally:
        await db.close()


# --- Conversation Logs ---


async def add_conversation_log(
    cid: str,
    turn_index: int,
    agent_raw: str,
    tool_calls: list,
    styles_after: list,
    injection: str,
    latency_ms: int,
):
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO conversation_logs (conversation_id, turn_index, agent_raw_output, tool_calls, active_moods_after, injection_block, agent_latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cid,
                turn_index,
                agent_raw,
                json.dumps(tool_calls),
                json.dumps(styles_after),
                injection,
                latency_ms,
                now,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def get_moods_before_turn(cid: str, turn_index: int) -> list[str]:
    """Return active_moods_after from the most recent log entry before turn_index."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT active_moods_after FROM conversation_logs WHERE conversation_id = ? AND turn_index < ? ORDER BY turn_index DESC LIMIT 1",
            (cid, turn_index),
        )
        if rows and rows[0]["active_moods_after"]:
            return json.loads(rows[0]["active_moods_after"])
        return []
    finally:
        await db.close()


async def get_conversation_logs(cid: str) -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM conversation_logs WHERE conversation_id = ? ORDER BY turn_index ASC",
            (cid,),
        )
        result = []
        for r in rows:
            d = dict(r)
            d["tool_calls"] = json.loads(d["tool_calls"]) if d["tool_calls"] else []
            d["active_moods_after"] = (
                json.loads(d["active_moods_after"]) if d["active_moods_after"] else []
            )
            result.append(d)
        return result
    finally:
        await db.close()


# --- Phrase Bank ---


async def get_phrase_bank() -> list[list[str]]:
    """Return phrase bank as list of variant groups (list of lists)."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT variants FROM phrase_bank ORDER BY id ASC"
        )
        return [json.loads(r["variants"]) for r in rows]
    finally:
        await db.close()


async def get_phrase_bank_rows() -> list[dict]:
    """Return phrase bank rows with ids for UI management."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, variants FROM phrase_bank ORDER BY id ASC"
        )
        return [{"id": r["id"], "variants": json.loads(r["variants"])} for r in rows]
    finally:
        await db.close()


async def add_phrase_group(variants: list[str]) -> int:
    """Add a new phrase variant group. Returns the new row id."""
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO phrase_bank (variants) VALUES (?)", (json.dumps(variants),)
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def update_phrase_group(group_id: int, variants: list[str]) -> bool:
    db = await get_db()
    try:
        cur = await db.execute(
            "UPDATE phrase_bank SET variants = ? WHERE id = ?",
            (json.dumps(variants), group_id),
        )
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def delete_phrase_group(group_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM phrase_bank WHERE id = ?", (group_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- User Personas ---


async def get_user_personas() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, name, description, avatar_color, created_at, updated_at FROM user_personas ORDER BY name ASC"
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_user_persona(persona_id: int) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, name, description, avatar_color, created_at, updated_at FROM user_personas WHERE id = ?",
            (persona_id,),
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_user_persona(data: dict) -> dict:
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "INSERT INTO user_personas (name, description, avatar_color, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                data["name"],
                data.get("description", ""),
                data.get("avatar_color"),
                now,
                now,
            ),
        )
        persona_id = cur.lastrowid
        await db.commit()
        return await get_user_persona(persona_id)
    finally:
        await db.close()


async def update_user_persona(persona_id: int, data: dict) -> dict | None:
    db = await get_db()
    try:
        allowed = ["name", "description", "avatar_color"]
        sets = []
        vals = []
        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(data[k])
        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
            vals.append(persona_id)
            await db.execute(
                f"UPDATE user_personas SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            await db.commit()
        return await get_user_persona(persona_id)
    finally:
        await db.close()


async def delete_user_persona(persona_id: int) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM user_personas WHERE id = ?", (persona_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- Character Cards ---


async def list_character_cards() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT id, name, description, personality, scenario, first_mes, creator_notes, system_prompt, tags, creator, source_format, created_at, updated_at, avatar_mime FROM character_cards ORDER BY updated_at DESC"
        )
        result = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d["tags"]) if d["tags"] else []
            d["has_avatar"] = d["avatar_mime"] is not None
            del d["avatar_mime"]
            result.append(d)
        return result
    finally:
        await db.close()


async def get_character_card(card_id: str, include_avatar: bool = False) -> dict | None:
    db = await get_db()
    try:
        cols = (
            "*"
            if include_avatar
            else (
                "id, name, description, personality, scenario, first_mes, mes_example, "
                "creator_notes, system_prompt, post_history_instructions, tags, creator, "
                "character_version, alternate_greetings, avatar_mime, source_format, created_at, updated_at"
            )
        )
        rows = await db.execute_fetchall(
            f"SELECT {cols} FROM character_cards WHERE id = ?",
            (card_id,),  # nosec B608 — cols is a hardcoded literal, not user input
        )
        if not rows:
            return None
        d = dict(rows[0])
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["alternate_greetings"] = (
            json.loads(d["alternate_greetings"]) if d.get("alternate_greetings") else []
        )
        d["has_avatar"] = d.get("avatar_mime") is not None
        return d
    finally:
        await db.close()


async def create_character_card(data: dict) -> dict:
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        try:
            await db.execute(
                """INSERT INTO character_cards
                   (id, name, description, personality, scenario, first_mes, mes_example,
                    creator_notes, system_prompt, post_history_instructions, tags, creator,
                    character_version, alternate_greetings, avatar_b64, avatar_mime,
                    source_format, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    data["id"],
                    data["name"],
                    data.get("description", ""),
                    data.get("personality", ""),
                    data.get("scenario", ""),
                    data.get("first_mes", ""),
                    data.get("mes_example", ""),
                    data.get("creator_notes", ""),
                    data.get("system_prompt", ""),
                    data.get("post_history_instructions", ""),
                    json.dumps(data.get("tags", [])),
                    data.get("creator", ""),
                    data.get("character_version", ""),
                    json.dumps(data.get("alternate_greetings", [])),
                    data.get("avatar_b64"),
                    data.get("avatar_mime"),
                    data.get("source_format", "manual"),
                    now,
                    now,
                ),
            )
        except aiosqlite.IntegrityError as exc:
            raise ValueError(
                f"Character card with id {data['id']} already exists"
            ) from exc
        await db.commit()
        return await get_character_card(data["id"])
    finally:
        await db.close()


async def insert_alternate_greeting_swipes(
    cid: str, alternate_greetings: list[str]
) -> int:
    """Insert alternate greeting swipes for a conversation in a single transaction.

    Swipe indices are assigned sequentially (1, 2, …) based on the order of
    non-empty greetings, skipping blanks. Swipe 0 is reserved for the
    materialised first_mes message created by the caller.

    Returns the number of swipes inserted.
    """
    if not alternate_greetings:
        return 0
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        swipe_index = 0
        for greeting in alternate_greetings:
            if greeting and greeting.strip():
                swipe_index += 1
                await db.execute(
                    "INSERT INTO messages "
                    "(conversation_id, role, content, turn_index, swipe_index, is_active, parent_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, 0, NULL, ?)",
                    (cid, "assistant", greeting.strip(), 0, swipe_index, now),
                )
        if swipe_index:
            await db.commit()
        return swipe_index
    finally:
        await db.close()


async def update_character_card(card_id: str, data: dict) -> dict | None:
    db = await get_db()
    try:
        allowed = [
            "name",
            "description",
            "personality",
            "scenario",
            "first_mes",
            "mes_example",
            "creator_notes",
            "system_prompt",
            "post_history_instructions",
            "creator",
            "character_version",
        ]
        sets = []
        vals = []
        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(data[k])
        # JSON fields
        if "tags" in data:
            sets.append("tags = ?")
            vals.append(json.dumps(data["tags"]))
        if "alternate_greetings" in data:
            sets.append("alternate_greetings = ?")
            vals.append(json.dumps(data["alternate_greetings"]))
        # Avatar
        if "avatar_b64" in data:
            sets.append("avatar_b64 = ?")
            vals.append(data["avatar_b64"])
            sets.append("avatar_mime = ?")
            vals.append(data.get("avatar_mime"))

        if sets:
            sets.append("updated_at = ?")
            vals.append(datetime.now(timezone.utc).isoformat())
            vals.append(card_id)
            await db.execute(
                f"UPDATE character_cards SET {', '.join(sets)} WHERE id = ?",
                vals,  # nosec B608 — cols from hardcoded allowlist, values parameterised
            )
            await db.commit()
        return await get_character_card(card_id)
    finally:
        await db.close()


async def sync_conversations_for_card(card_id: str, card: dict) -> None:
    """Propagate mutable card fields to all conversations linked to this card.

    Only syncs fields that are denormalised onto the conversation row and
    affect prompt-building at runtime. first_mes is excluded because it has
    already been materialised as a message in the conversation tree.
    """
    db = await get_db()
    try:
        await db.execute(
            """UPDATE conversations
               SET character_name = ?,
                   character_scenario = ?,
                   post_history_instructions = ?
               WHERE character_card_id = ?""",
            (
                card.get("name", ""),
                card.get("scenario", ""),
                card.get("post_history_instructions", ""),
                card_id,
            ),
        )
        await db.commit()
    finally:
        await db.close()


async def delete_character_card(
    card_id: str, delete_conversations: bool = False
) -> bool:
    db = await get_db()
    try:
        if delete_conversations:
            await db.execute(
                "DELETE FROM conversations WHERE character_card_id = ?", (card_id,)
            )
        # When keeping conversations, character_card_id is intentionally left as-is.
        # The dangling reference acts as a pending-relink marker: re-importing the
        # same card (which produces the same stable ID) restores the association
        # automatically. resolve_char_context() handles a missing card gracefully.
        cur = await db.execute("DELETE FROM character_cards WHERE id = ?", (card_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def delete_message_with_descendants(cid: str, msg_id: int) -> bool:
    """Delete a message, all its siblings, and all their descendants. Updates active_leaf_id if the active branch is affected."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT parent_id FROM messages WHERE id = ? AND conversation_id = ?",
            (msg_id, cid),
        )
        if not rows:
            return False
        parent_id = rows[0]["parent_id"]

        # Collect all siblings (messages with the same parent_id) and their descendants via recursive CTE
        # For root messages (parent_id IS NULL), match other root messages
        if parent_id is not None:
            sibling_cond = "parent_id = ?"
            sibling_params = (parent_id,)
        else:
            sibling_cond = "parent_id IS NULL"
            sibling_params = ()

        desc_rows = await db.execute_fetchall(
            f"""
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM messages WHERE conversation_id = ? AND {sibling_cond}
                UNION ALL
                SELECT m.id FROM messages m
                INNER JOIN subtree s ON m.parent_id = s.id
                WHERE m.conversation_id = ?
            )
            SELECT id FROM subtree
        """,
            (cid, *sibling_params, cid),
        )
        deleted_ids = {r["id"] for r in desc_rows}

        if not deleted_ids:
            return False

        # If the active leaf is inside the deleted subtree, find a new active leaf
        # Since all siblings are deleted, the new active leaf will be the parent (or NULL for root)
        conv_rows = await db.execute_fetchall(
            "SELECT active_leaf_id FROM conversations WHERE id = ?", (cid,)
        )
        if conv_rows and conv_rows[0]["active_leaf_id"] in deleted_ids:
            new_leaf = parent_id  # parent_id is None for root messages, which is valid

            await db.execute(
                "UPDATE conversations SET active_leaf_id = ? WHERE id = ?",
                (new_leaf, cid),
            )

        placeholders = ",".join("?" * len(deleted_ids))
        await db.execute(
            f"DELETE FROM messages WHERE id IN ({placeholders})",  # nosec B608 — placeholders is only '?' chars, ids are parameterised
            list(deleted_ids),
        )

        # Restore director_state to match the new active leaf's turn
        conv_after = await db.execute_fetchall(
            "SELECT active_leaf_id FROM conversations WHERE id = ?", (cid,)
        )
        new_leaf_id = conv_after[0]["active_leaf_id"] if conv_after else None
        if new_leaf_id is not None:
            leaf_row = await db.execute_fetchall(
                "SELECT turn_index FROM messages WHERE id = ?", (new_leaf_id,)
            )
            if leaf_row:
                turn_idx = leaf_row[0]["turn_index"]
                log_row = await db.execute_fetchall(
                    "SELECT active_moods_after FROM conversation_logs WHERE conversation_id = ? AND turn_index = ? ORDER BY id DESC LIMIT 1",
                    (cid, turn_idx),
                )
                restored = (
                    json.loads(log_row[0]["active_moods_after"])
                    if log_row and log_row[0]["active_moods_after"]
                    else []
                )
                await db.execute(
                    "UPDATE director_state SET active_moods = ? WHERE conversation_id = ?",
                    (json.dumps(restored), cid),
                )
        else:
            # No messages left; reset styles
            await db.execute(
                "UPDATE director_state SET active_moods = '[]' WHERE conversation_id = ?",
                (cid,),
            )

        await db.commit()
        return True
    finally:
        await db.close()


async def resolve_char_context(conv: dict, settings: dict) -> tuple[str, str, str]:
    """Load character card data and resolve the effective system prompt, persona, and example messages.

    Combines shared_system_prompt (global) with system_prompt (model-specific).
    Character card system_prompt, if present, completely overrides both.

    Returns (system_prompt, char_persona, mes_example).
    """
    # Combine shared (global) + model-specific system prompts
    shared = settings.get("shared_system_prompt", "")
    model_specific = settings.get("system_prompt", "")

    if shared and model_specific:
        system_prompt = f"{shared}\n\n{model_specific}"
    else:
        system_prompt = shared or model_specific

    char_persona, mes_example = "", ""
    if card_id := conv.get("character_card_id"):
        card = await get_character_card(card_id)
        if card:
            char_persona = "\n\n".join(
                filter(None, [card.get("description", ""), card.get("personality", "")])
            )
            mes_example = card.get("mes_example", "")
            if card.get("system_prompt"):
                # Character card system_prompt completely overrides
                system_prompt = card["system_prompt"]
    return system_prompt, char_persona, mes_example


async def get_character_avatar(card_id: str) -> tuple[bytes, str] | None:
    """Returns (image_bytes, mime_type) or None."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT avatar_b64, avatar_mime FROM character_cards WHERE id = ?",
            (card_id,),
        )
        if not rows or not rows[0]["avatar_b64"]:
            return None
        import base64

        return base64.b64decode(rows[0]["avatar_b64"]), rows[0]["avatar_mime"]
    finally:
        await db.close()


# --- Reset to Defaults ---


async def reset_to_defaults() -> None:
    """Delete all user-modified data and re-seed tables to defaults."""
    db = await get_db()
    try:
        await db.execute("DELETE FROM settings WHERE id = 1")
        await db.execute("DELETE FROM mood_fragments")
        await db.execute("DELETE FROM director_fragments")
        await db.execute("DELETE FROM phrase_bank")
        await db.execute("DELETE FROM model_configs")
        await db.execute("DELETE FROM endpoints")

        # Re-seed settings
        s = DEFAULT_SETTINGS
        await db.execute(
            "INSERT INTO settings (id, endpoint_url, model_name, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, shared_system_prompt, system_prompt, enabled_tools) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                s["endpoint_url"],
                s["model_name"],
                s["temperature"],
                s["min_p"],
                s["top_k"],
                s["top_p"],
                s["repetition_penalty"],
                s["max_tokens"],
                s["shared_system_prompt"],
                s["system_prompt"],
                json.dumps(DEFAULT_ENABLED_TOOLS),
            ),
        )

        # Re-seed endpoint from default settings
        cur_ep = await db.execute(
            "INSERT INTO endpoints (url, api_key) VALUES (?, ?)",
            (s["endpoint_url"], ""),
        )
        endpoint_id = cur_ep.lastrowid
        cur_mc = await db.execute(
            "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                endpoint_id,
                s["model_name"],
                "",  # Model-specific system_prompt starts empty
                s["temperature"],
                s["min_p"],
                s["top_k"],
                s["top_p"],
                s["repetition_penalty"],
                s["max_tokens"],
            ),
        )
        model_config_id = cur_mc.lastrowid
        await db.execute(
            "UPDATE endpoints SET active_model_config_id = ? WHERE id = ?",
            (model_config_id, endpoint_id),
        )
        await db.execute(
            "UPDATE settings SET active_endpoint_id = ? WHERE id = 1",
            (endpoint_id,),
        )

        # Re-seed mood fragments
        for f in SEED_MOOD_FRAGMENTS:
            await db.execute(
                "INSERT INTO mood_fragments (id, label, description, prompt_text, negative_prompt) VALUES (?, ?, ?, ?, ?)",
                (
                    f["id"],
                    f["label"],
                    f["description"],
                    f["prompt_text"],
                    f["negative_prompt"],
                ),
            )

        # Re-seed director fragments
        for df in SEED_DIRECTOR_FRAGMENTS:
            await db.execute(
                "INSERT INTO director_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    df["id"],
                    df["label"],
                    df["description"],
                    df["field_type"],
                    1 if df["required"] else 0,
                    df["injection_label"],
                    df["sort_order"],
                ),
            )

        # Re-seed phrase bank
        for variants in SEED_PHRASE_BANK:
            await db.execute(
                "INSERT INTO phrase_bank (variants) VALUES (?)",
                (json.dumps(variants),),
            )

        await db.commit()
    finally:
        await db.close()
