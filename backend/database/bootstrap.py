from __future__ import annotations

import json
from datetime import UTC, datetime

from ..core import DECISION_COLUMNS, STATE_COLUMNS
from . import connection
from .connection import get_db
from .migrations import run_pending, stamp_all
from .schema import CREATE_TABLES_SQL
from .seeds import (
    DEFAULT_CONNECTION,
    DEFAULT_ENABLED_TOOLS,
    DEFAULT_SETTINGS,
    SEED_INTERACTIVE_FRAGMENTS,
    SEED_MOOD_FRAGMENTS,
    SEED_PHRASE_BANK,
)


async def init_db() -> int:
    """Initialize or upgrade the database, then seed it.

    Without application schema, install schema, seeds and migration baseline atomically. Existing databases migrate before
    schema indexes can reference new columns. Return the migration count for startup page reclamation.
    """
    async with get_db() as db:
        existing = await db.execute_fetchall("SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1")
        migrated = run_pending(connection.DB_PATH) if existing else 0
        # executescript otherwise commits DDL independently of seeds. Keeping the BEGIN inside the script lets connection close
        # roll everything back on any failure, including a cancelled first startup.
        await db.executescript("BEGIN IMMEDIATE;\n" + CREATE_TABLES_SQL)

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM settings"))
        if row[0]["c"] == 0:
            await _seed_settings(db)
            await _seed_default_persona(db)

        ep_row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM endpoints"))
        if ep_row[0]["c"] == 0:
            await _seed_default_endpoint(db)

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM mood_fragments"))
        if row[0]["c"] == 0:
            await _seed_mood_fragments(db)

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM interactive_fragments"))
        if row[0]["c"] == 0:
            await _seed_interactive_fragments(db)

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM phrase_bank"))
        if row[0]["c"] == 0:
            await _seed_phrase_bank(db)

        if not existing:
            await stamp_all(db)
        await db.commit()
    return migrated


async def reset_to_defaults() -> None:
    """Reset user data and re-seed default rows."""
    async with get_db() as db:
        # Carry attachment-cache bookkeeping across the settings rebuild.
        rows = list(
            await db.execute_fetchall(
                "SELECT attachment_cache_budget_bytes, attachment_access_counter FROM settings WHERE id = 1"
            )
        )
        cache_bookkeeping = dict(rows[0]) if rows else None

        await db.execute("DELETE FROM settings WHERE id = 1")
        await db.execute("DELETE FROM mood_fragments")
        await db.execute("DELETE FROM interactive_fragments")
        await db.execute("DELETE FROM phrase_bank")
        # Suggestions, dismissals and the last run's status belong to the bank they were mined against.
        await db.execute("DELETE FROM slop_suggestions")
        await db.execute("DELETE FROM slop_dismissals")
        await db.execute("DELETE FROM slop_mining_state")
        await db.execute("DELETE FROM model_configs")
        await db.execute("DELETE FROM endpoints")

        await _seed_settings(db)
        await _seed_default_endpoint(db)
        await _seed_mood_fragments(db)
        await _seed_interactive_fragments(db)
        await _seed_phrase_bank(db)

        if cache_bookkeeping is not None:
            await db.execute(
                "UPDATE settings SET attachment_cache_budget_bytes = ?, attachment_access_counter = ? WHERE id = 1",
                (cache_bookkeeping["attachment_cache_budget_bytes"], cache_bookkeeping["attachment_access_counter"]),
            )

        await db.commit()


async def _seed_settings(db) -> None:
    s = DEFAULT_SETTINGS
    await db.execute(
        "INSERT INTO settings (id, shared_system_prompt, system_prompt, enabled_tools) VALUES (1, ?, ?, ?)",
        (s["shared_system_prompt"], s["system_prompt"], json.dumps(DEFAULT_ENABLED_TOOLS)),
    )


async def _seed_default_persona(db) -> None:
    """Create the default persona and link it as active (was migration 0003)."""
    now = datetime.now(UTC).isoformat()
    cur = await db.execute(
        "INSERT INTO user_personas (name, description, avatar_color, created_at, updated_at) VALUES ('User', '', '#3b82f6', ?, ?)",
        (now, now),
    )
    await db.execute("UPDATE settings SET active_persona_id = ? WHERE id = 1", (cur.lastrowid,))


async def _seed_default_endpoint(db) -> None:
    """Create the default endpoint with Writer and Agent model configs, and make it the active endpoint."""
    c = DEFAULT_CONNECTION
    cur = await db.execute("INSERT INTO endpoints (url, api_key) VALUES (?, ?)", (c["endpoint_url"], c["api_key"]))
    endpoint_id = cur.lastrowid
    config_ids = []
    for role in ("writer", "agent"):
        config = await db.execute(
            "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, ?)",
            (
                endpoint_id,
                c["model_name"],
                c["temperature"],
                c["min_p"],
                c["top_k"],
                c["top_p"],
                c["repetition_penalty"],
                c["max_tokens"],
                role,
            ),
        )
        config_ids.append(config.lastrowid)
    await db.execute(
        "UPDATE endpoints SET active_model_config_id = ?, agent_active_model_config_id = ? WHERE id = ?",
        (*config_ids, endpoint_id),
    )
    await db.execute("UPDATE settings SET active_endpoint_id = ? WHERE id = 1", (endpoint_id,))


async def _seed_mood_fragments(db) -> None:
    for f in SEED_MOOD_FRAGMENTS:
        await db.execute(
            "INSERT INTO mood_fragments (id, label, description, prompt_text, negative_prompt) VALUES (?, ?, ?, ?, ?)",
            (f["id"], f["label"], f["description"], f["prompt_text"], f["negative_prompt"]),
        )


async def _seed_interactive_fragments(db) -> None:
    columns = ("id", "label", "description", "field_type", "required", "enabled", "injection_label", "sort_order")
    columns += DECISION_COLUMNS + STATE_COLUMNS
    for df in SEED_INTERACTIVE_FRAGMENTS:
        row = {"description": "", "enabled": True, **df}
        values = [row.get(column) for column in columns]
        await db.execute(
            f"INSERT INTO interactive_fragments ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",  # nosec B608
            [json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for value in values],
        )


async def _seed_phrase_bank(db) -> None:
    # A seed entry is either a raw regex pattern (str) or a list of literal variant phrases.
    for entry in SEED_PHRASE_BANK:
        if isinstance(entry, str):
            await db.execute(
                "INSERT INTO phrase_bank (variants, kind, pattern) VALUES (?, 'regex', ?)", (json.dumps([]), entry)
            )
        else:
            await db.execute(
                "INSERT INTO phrase_bank (variants, kind, pattern) VALUES (?, 'literal', NULL)", (json.dumps(entry),)
            )
