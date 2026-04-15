from __future__ import annotations
import aiosqlite
import json
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "app.db")

SEED_FRAGMENTS = [
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
        "description": "Foreground the character's inner thoughts. To be used SPARINGLY!",
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
        "description": "Short, punchy prose with no filler, like a haiku.",
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
        "description": "NSFW mode - descriptive, enable if the vibes of the situation call for it",
        "prompt_text": (
            'The narration may describe body parts that are exposed or looked at - visually, or interacted with - tactilely, but sparingly in order to avoid repetition. Take initiative and actively push the scene forward, no need to ask. '
            'The narration can use NSFW, vulgar, uncensored, explicit vocabulary where applicable. Avoid roundabout euphemisms like "heat", "core", etc. The prose is now more brutal, vivid, and direct. '
            'Consider positions and clothing states of the characters - what are they wearing, and where are they in relation to each other?. '
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
    "system_prompt": "You are a creative roleplay partner. Be responsive to the scene's evolving tone.\nCharacters have their own conviction and ideas, they may disagree with each other.\nKeep tenses (past, present) and POV consistent.\nAvoid repetition of word choices and sentence structures.",
    "user_name": "User",
    "user_description": "",
    "enable_agent": True,
    "length_guard_max_words": 240,
    "length_guard_max_paragraphs": 4,
}

SEED_PHRASE_BANK = [
    ["a mix of", "a mixture of"],
    ["dripped with", "dripping with"],
    ["the tension in the air", "thick tension in the air", "the air is heavy", "the air is charged", "the air is thick"],
    ["filling the air", "fills the air", "filled the air"],
    ["hang in the air", "hung in the air", "hangs in the air", "hanging in the air"],
    ["dangerous voice", "dangerous tone"],
    ["voice dropping", "voice low", "voice dangerous", "voice a dangerous", "voice a low", "voice is a low", "voice is a dangerous"],
    ["low hiss", "dangerous hiss", "barely a whisper", "barely above a whisper"],
    ["voice cracks", "voice cracking", "voice cracked"],
    ["a low, guttural", "a guttural sound"],
    ["a predatory smirk", "I don't bite", "they don't bite", "it doesn't bite", "predatory glee"],
    ["very brave or very stupid", "either very brave or very foolish", "brave or stupid"],
    ["sending shivers", "sending a shiver"],
    ["a dance of", "a dance between", "dancing with"],
    ["eyes narrowing", "eyes narrowed", "mischievous glint", "gaze sharpen", "eyes widen", "glint with mischief", "eyes wide"],
    ["eyes never leaving his", "eyes never leaving hers", "eyes never leave his", "eyes never leave hers"],
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
    ["the air between them", "thick and suffocating", "thick, suffocating"],
    ["mind races", "mind racing"],
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
        await db.executescript("""
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
                system_prompt TEXT NOT NULL DEFAULT '',
                user_name TEXT NOT NULL DEFAULT 'User',
                user_description TEXT NOT NULL DEFAULT '',
                enabled_tools TEXT NOT NULL DEFAULT '{}',
                enable_agent INTEGER NOT NULL DEFAULT 1,
                length_guard_max_words INTEGER NOT NULL DEFAULT 240,
                length_guard_max_paragraphs INTEGER NOT NULL DEFAULT 4,
                reasoning_enabled_passes TEXT NOT NULL DEFAULT '{"director":true,"writer":true,"refiner":true}'
            );

            CREATE TABLE IF NOT EXISTS fragments (
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

            CREATE TABLE IF NOT EXISTS model_token_cache (
                model_key TEXT PRIMARY KEY,
                tool_start_token_id INTEGER  -- NULL = probed but no usable token found
            );
        """)

        # Migrations for existing DBs
        existing_cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(settings)")}
        if "enable_agent" not in existing_cols:
            await db.execute("ALTER TABLE settings ADD COLUMN enable_agent INTEGER NOT NULL DEFAULT 1")
        if "length_guard_max_words" not in existing_cols:
            await db.execute("ALTER TABLE settings ADD COLUMN length_guard_max_words INTEGER NOT NULL DEFAULT 400")
        if "length_guard_max_paragraphs" not in existing_cols:
            await db.execute("ALTER TABLE settings ADD COLUMN length_guard_max_paragraphs INTEGER NOT NULL DEFAULT 5")
        if "reasoning_enabled_passes" not in existing_cols:
            await db.execute("ALTER TABLE settings ADD COLUMN reasoning_enabled_passes TEXT NOT NULL DEFAULT '{\"director\":true,\"writer\":true,\"refiner\":true}'")

        # Migration for director_state keywords column
        director_cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(director_state)")}
        if "keywords" not in director_cols:
            await db.execute("ALTER TABLE director_state ADD COLUMN keywords TEXT NOT NULL DEFAULT '[]'")
        
        # Migration for fragments enabled column
        fragment_cols = {row[1] for row in await db.execute_fetchall("PRAGMA table_info(fragments)")}
        if "enabled" not in fragment_cols:
            await db.execute("ALTER TABLE fragments ADD COLUMN enabled BOOLEAN NOT NULL DEFAULT 1")

        # Seed settings if empty
        row = await db.execute_fetchall("SELECT COUNT(*) as c FROM settings")
        if row[0]["c"] == 0:
            s = DEFAULT_SETTINGS
            await db.execute(
                "INSERT INTO settings (id, endpoint_url, model_name, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, system_prompt) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s["endpoint_url"], s["model_name"], s["temperature"], s["min_p"], s["top_k"], s["top_p"], s["repetition_penalty"], s["max_tokens"], s["system_prompt"]),
            )

        # Seed fragments if empty
        row = await db.execute_fetchall("SELECT COUNT(*) as c FROM fragments")
        if row[0]["c"] == 0:
            for f in SEED_FRAGMENTS:
                await db.execute(
                    "INSERT INTO fragments (id, label, description, prompt_text, negative_prompt) VALUES (?, ?, ?, ?, ?)",
                    (f["id"], f["label"], f["description"], f["prompt_text"], f["negative_prompt"]),
                )

        # Seed phrase_bank if empty
        row = await db.execute_fetchall("SELECT COUNT(*) as c FROM phrase_bank")
        if row[0]["c"] == 0:
            for variants in SEED_PHRASE_BANK:
                await db.execute("INSERT INTO phrase_bank (variants) VALUES (?)", (json.dumps(variants),))

        await db.commit()

    finally:
        await db.close()


# --- Settings ---

async def get_settings() -> dict:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM settings WHERE id = 1")
        if not rows:
            return DEFAULT_SETTINGS
        s = dict(rows[0])
        s["enabled_tools"] = json.loads(s.get("enabled_tools") or "{}")
        s["reasoning_enabled_passes"] = json.loads(s.get("reasoning_enabled_passes") or '{"director":true,"writer":true,"refiner":true}')
        return s
    finally:
        await db.close()


async def update_settings(data: dict) -> dict:
    db = await get_db()
    try:
        allowed = ["endpoint_url", "api_key", "model_name", "temperature", "min_p", "top_k", "top_p", "repetition_penalty", "max_tokens", "system_prompt", "user_name", "user_description", "enabled_tools", "enable_agent", "length_guard_max_words", "length_guard_max_paragraphs", "reasoning_enabled_passes"]
        json_fields = {"enabled_tools", "reasoning_enabled_passes"}
        sets = []
        vals = []

        for k in allowed:
            if k in data:
                sets.append(f"{k} = ?")
                vals.append(json.dumps(data[k]) if k in json_fields else data[k])
        if sets:
            await db.execute(f"UPDATE settings SET {', '.join(sets)} WHERE id = 1", vals)
            await db.commit()
        return await get_settings()
    finally:
        await db.close()


# --- Model token cache ---

async def get_tool_start_token(model_key: str) -> tuple[bool, int | None]:
    """Return (was_cached, token_id).

    was_cached=True means the model has been probed before (token_id may be
    None if the probe found no usable control token).
    was_cached=False means this model has never been probed.
    """
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT tool_start_token_id FROM model_token_cache WHERE model_key = ?", (model_key,)
        )
        if not rows:
            return False, None
        raw = rows[0]["tool_start_token_id"]
        return True, int(raw) if raw is not None else None
    finally:
        await db.close()


async def set_tool_start_token(model_key: str, token_id: int | None) -> None:
    """Cache the tool-start token ID for a model key.

    Pass token_id=None to record that the model was probed but has no
    usable control token (suppresses future re-probing).
    """
    db = await get_db()
    try:
        await db.execute(
            "INSERT OR REPLACE INTO model_token_cache (model_key, tool_start_token_id) VALUES (?, ?)",
            (model_key, token_id),
        )
        await db.commit()
    finally:
        await db.close()


# --- Fragments ---

async def get_fragments() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM fragments ORDER BY label ASC")
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_fragment(fid: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM fragments WHERE id = ?", (fid,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_fragment(data: dict) -> dict:
    db = await get_db()
    try:
        enabled = data.get("enabled", 1)
        await db.execute(
            "INSERT INTO fragments (id, label, description, prompt_text, negative_prompt, enabled) VALUES (?, ?, ?, ?, ?, ?)",
            (data["id"], data["label"], data["description"], data["prompt_text"], data.get("negative_prompt", ""), enabled),
        )
        await db.commit()
        return await get_fragment(data["id"])
    finally:
        await db.close()


async def update_fragment(fid: str, data: dict) -> dict | None:
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
            await db.execute(f"UPDATE fragments SET {', '.join(sets)} WHERE id = ?", vals)
            await db.commit()
        return await get_fragment(fid)
    finally:
        await db.close()


async def delete_fragment(fid: str) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("DELETE FROM fragments WHERE id = ?", (fid,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


# --- Conversations ---

async def list_conversations() -> list[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("""
            SELECT c.*,
                   (SELECT m.content FROM messages m
                    WHERE m.conversation_id = c.id
                    ORDER BY m.id DESC LIMIT 1) AS last_message_preview
            FROM conversations c
            ORDER BY COALESCE(c.updated_at, c.created_at) DESC
        """)
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_conversation(cid: str) -> dict | None:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM conversations WHERE id = ?", (cid,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def create_conversation(
    cid: str, title: str, char_name: str, char_scenario: str,
    first_mes: str = "", post_history_instructions: str = "",
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
            (cid, title, character_card_id, char_name, char_scenario,
             first_mes, post_history_instructions, now, now),
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
    return await _get_path_to_leaf(cid, leaf_id)


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
            msg["next_branch_id"] = sibling_ids[idx + 1] if idx < len(sibling_ids) - 1 else None
        return messages
    finally:
        await db.close()


async def get_messages_with_swipe_info(cid: str) -> list[dict]:
    """Backward-compat alias for get_messages_with_branch_info."""
    return await get_messages_with_branch_info(cid)


async def get_swipes_at_turn(cid: str, turn_index: int) -> list[dict]:
    """Get all swipes at a specific turn_index."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM messages WHERE conversation_id = ? AND turn_index = ? ORDER BY swipe_index ASC",
            (cid, turn_index),
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def add_message(cid: str, role: str, content: str, turn_index: int, swipe_index: int = 0, parent_id: int | None = None) -> int:
    """Add a message. Returns the new message id."""
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        cur = await db.execute(
            "INSERT INTO messages (conversation_id, role, content, turn_index, swipe_index, is_active, parent_id, created_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
            (cid, role, content, turn_index, swipe_index, parent_id, now),
        )
        await db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, cid))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def update_message_content(msg_id: int, content: str) -> None:
    """Update the content of an existing message."""
    db = await get_db()
    try:
        await db.execute("UPDATE messages SET content = ? WHERE id = ?", (content, msg_id))
        await db.commit()
    finally:
        await db.close()


async def get_message_by_id(msg_id: int) -> dict | None:
    """Fetch a single message by its primary key."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM messages WHERE id = ?", (msg_id,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def set_active_leaf(cid: str, leaf_id: int | None):
    """Update the active_leaf_id for a conversation."""
    db = await get_db()
    try:
        await db.execute("UPDATE conversations SET active_leaf_id = ? WHERE id = ?", (leaf_id, cid))
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
        rows = await db.execute_fetchall("SELECT * FROM director_state WHERE conversation_id = ?", (cid,))
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


async def update_director_state(cid: str, active_moods: list, keywords: list | None = None):
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

async def add_conversation_log(cid: str, turn_index: int, agent_raw: str, tool_calls: list, styles_after: list, injection: str, latency_ms: int):
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO conversation_logs (conversation_id, turn_index, agent_raw_output, tool_calls, active_moods_after, injection_block, agent_latency_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (cid, turn_index, agent_raw, json.dumps(tool_calls), json.dumps(styles_after), injection, latency_ms, now),
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
            d["active_moods_after"] = json.loads(d["active_moods_after"]) if d["active_moods_after"] else []
            result.append(d)
        return result
    finally:
        await db.close()


# --- Phrase Bank ---

async def get_phrase_bank() -> list[list[str]]:
    """Return phrase bank as list of variant groups (list of lists)."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT variants FROM phrase_bank ORDER BY id ASC")
        return [json.loads(r["variants"]) for r in rows]
    finally:
        await db.close()


async def get_phrase_bank_rows() -> list[dict]:
    """Return phrase bank rows with ids for UI management."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT id, variants FROM phrase_bank ORDER BY id ASC")
        return [{"id": r["id"], "variants": json.loads(r["variants"])} for r in rows]
    finally:
        await db.close()


async def add_phrase_group(variants: list[str]) -> int:
    """Add a new phrase variant group. Returns the new row id."""
    db = await get_db()
    try:
        cur = await db.execute("INSERT INTO phrase_bank (variants) VALUES (?)", (json.dumps(variants),))
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def update_phrase_group(group_id: int, variants: list[str]) -> bool:
    db = await get_db()
    try:
        cur = await db.execute("UPDATE phrase_bank SET variants = ? WHERE id = ?", (json.dumps(variants), group_id))
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
        cols = "*" if include_avatar else (
            "id, name, description, personality, scenario, first_mes, mes_example, "
            "creator_notes, system_prompt, post_history_instructions, tags, creator, "
            "character_version, alternate_greetings, avatar_mime, source_format, created_at, updated_at"
        )
        rows = await db.execute_fetchall(f"SELECT {cols} FROM character_cards WHERE id = ?", (card_id,))
        if not rows:
            return None
        d = dict(rows[0])
        d["tags"] = json.loads(d["tags"]) if d.get("tags") else []
        d["alternate_greetings"] = json.loads(d["alternate_greetings"]) if d.get("alternate_greetings") else []
        d["has_avatar"] = d.get("avatar_mime") is not None
        return d
    finally:
        await db.close()


async def create_character_card(data: dict) -> dict:
    db = await get_db()
    try:
        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            """INSERT INTO character_cards
               (id, name, description, personality, scenario, first_mes, mes_example,
                creator_notes, system_prompt, post_history_instructions, tags, creator,
                character_version, alternate_greetings, avatar_b64, avatar_mime,
                source_format, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["id"], data["name"], data.get("description", ""),
                data.get("personality", ""), data.get("scenario", ""),
                data.get("first_mes", ""), data.get("mes_example", ""),
                data.get("creator_notes", ""), data.get("system_prompt", ""),
                data.get("post_history_instructions", ""),
                json.dumps(data.get("tags", [])), data.get("creator", ""),
                data.get("character_version", ""),
                json.dumps(data.get("alternate_greetings", [])),
                data.get("avatar_b64"), data.get("avatar_mime"),
                data.get("source_format", "manual"), now, now,
            ),
        )
        await db.commit()
        return await get_character_card(data["id"])
    finally:
        await db.close()


async def update_character_card(card_id: str, data: dict) -> dict | None:
    db = await get_db()
    try:
        allowed = [
            "name", "description", "personality", "scenario", "first_mes",
            "mes_example", "creator_notes", "system_prompt", "post_history_instructions",
            "creator", "character_version",
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
            await db.execute(f"UPDATE character_cards SET {', '.join(sets)} WHERE id = ?", vals)
            await db.commit()
        return await get_character_card(card_id)
    finally:
        await db.close()


async def delete_character_card(card_id: str, delete_conversations: bool = False) -> bool:
    db = await get_db()
    try:
        if delete_conversations:
            await db.execute("DELETE FROM conversations WHERE character_card_id = ?", (card_id,))
        cur = await db.execute("DELETE FROM character_cards WHERE id = ?", (card_id,))
        await db.commit()
        return cur.rowcount > 0
    finally:
        await db.close()


async def delete_message_with_descendants(cid: str, msg_id: int) -> bool:
    """Delete a message and all its descendants. Updates active_leaf_id if the active branch is affected."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT parent_id FROM messages WHERE id = ? AND conversation_id = ?",
            (msg_id, cid),
        )
        if not rows:
            return False
        parent_id = rows[0]["parent_id"]

        # Collect the full subtree to delete via recursive CTE
        desc_rows = await db.execute_fetchall("""
            WITH RECURSIVE subtree(id) AS (
                SELECT id FROM messages WHERE id = ? AND conversation_id = ?
                UNION ALL
                SELECT m.id FROM messages m
                INNER JOIN subtree s ON m.parent_id = s.id
                WHERE m.conversation_id = ?
            )
            SELECT id FROM subtree
        """, (msg_id, cid, cid))
        deleted_ids = {r["id"] for r in desc_rows}

        if not deleted_ids:
            return False

        # If the active leaf is inside the deleted subtree, find a new active leaf
        conv_rows = await db.execute_fetchall(
            "SELECT active_leaf_id FROM conversations WHERE id = ?", (cid,)
        )
        if conv_rows and conv_rows[0]["active_leaf_id"] in deleted_ids:
            new_leaf = parent_id
            # Prefer a surviving sibling branch over stopping at the bare parent
            # Handle both cases: parent_id is None (root messages) and parent_id is not None
            if parent_id is not None:
                sibling_query = "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? AND id != ? ORDER BY id ASC LIMIT 1"
                sibling_params = (cid, parent_id, msg_id)
            else:
                # For root messages, look for other root messages (parent_id IS NULL)
                sibling_query = "SELECT id FROM messages WHERE conversation_id = ? AND parent_id IS NULL AND id != ? ORDER BY id ASC LIMIT 1"
                sibling_params = (cid, msg_id)
            
            sibling_rows = await db.execute_fetchall(sibling_query, sibling_params)
            if sibling_rows:
                # Walk to the deepest descendant of that sibling
                candidate = sibling_rows[0]["id"]
                while True:
                    child_rows = await db.execute_fetchall(
                        "SELECT id FROM messages WHERE conversation_id = ? AND parent_id = ? ORDER BY id DESC LIMIT 1",
                        (cid, candidate),
                    )
                    if not child_rows:
                        break
                    candidate = child_rows[0]["id"]
                new_leaf = candidate
            
            await db.execute(
                "UPDATE conversations SET active_leaf_id = ? WHERE id = ?",
                (new_leaf, cid),
            )

        placeholders = ",".join("?" * len(deleted_ids))
        await db.execute(
            f"DELETE FROM messages WHERE id IN ({placeholders})",
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
                restored = json.loads(log_row[0]["active_moods_after"]) if log_row and log_row[0]["active_moods_after"] else []
                await db.execute(
                    "UPDATE director_state SET active_moods = ? WHERE conversation_id = ?",
                    (json.dumps(restored), cid),
                )
        else:
            # No messages left; reset styles
            await db.execute(
                "UPDATE director_state SET active_moods = '[]' WHERE conversation_id = ?", (cid,)
            )

        await db.commit()
        return True
    finally:
        await db.close()


async def resolve_char_context(conv: dict, settings: dict) -> tuple[str, str, str]:
    """Load character card data and resolve the effective system prompt, persona, and example messages.

    Returns (system_prompt, char_persona, mes_example).
    """
    system_prompt = settings["system_prompt"]
    char_persona, mes_example = "", ""
    if card_id := conv.get("character_card_id"):
        card = await get_character_card(card_id)
        if card:
            char_persona = "\n\n".join(filter(None, [card.get("description", ""), card.get("personality", "")]))
            mes_example = card.get("mes_example", "")
            if card.get("system_prompt"):
                system_prompt = card["system_prompt"]
    return system_prompt, char_persona, mes_example


async def get_character_avatar(card_id: str) -> tuple[bytes, str] | None:
    """Returns (image_bytes, mime_type) or None."""
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT avatar_b64, avatar_mime FROM character_cards WHERE id = ?", (card_id,)
        )
        if not rows or not rows[0]["avatar_b64"]:
            return None
        import base64
        return base64.b64decode(rows[0]["avatar_b64"]), rows[0]["avatar_mime"]
    finally:
        await db.close()