from __future__ import annotations

import json
from datetime import UTC, datetime

from . import connection
from .connection import get_db
from .migrations import run_pending, stamp_all
from .queries.active_profile import get_active_profile_id
from .queries.profiles import reset_profile, seed_default_profile
from .schema import CREATE_TABLES_SQL
from .seeds import DEFAULT_CONNECTION, DEFAULT_ENABLED_TOOLS, SEED_PROFILE_NAME


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

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM profiles"))
        if row[0]["c"] == 0:
            # Last, so its config captures the seeded settings and endpoint. It seeds the fragments and phrase bank under it.
            await seed_default_profile(db, SEED_PROFILE_NAME)

        if not existing:
            await stamp_all(db)
        await db.commit()
    return migrated


async def reset_to_defaults() -> None:
    """Reset the shared settings and endpoints, and the active profile's layout, library and switches, to the seeded ones.
    Other profiles keep theirs."""
    active = await get_active_profile_id()
    async with get_db() as db:
        # Carry attachment-cache bookkeeping across the settings rebuild.
        rows = list(
            await db.execute_fetchall(
                "SELECT attachment_cache_budget_bytes, attachment_access_counter FROM settings WHERE id = 1"
            )
        )
        cache_bookkeeping = dict(rows[0]) if rows else None

        await db.execute("DELETE FROM settings WHERE id = 1")
        await db.execute("DELETE FROM model_configs")
        await db.execute("DELETE FROM endpoints")

        await _seed_settings(db)
        await _seed_default_endpoint(db)
        if list(await db.execute_fetchall("SELECT 1 FROM profiles WHERE id = ?", (active,))):
            await reset_profile(db, active)
            await db.execute("UPDATE settings SET active_profile_id = ? WHERE id = 1", (active,))
        else:
            await seed_default_profile(db, SEED_PROFILE_NAME)

        if cache_bookkeeping is not None:
            await db.execute(
                "UPDATE settings SET attachment_cache_budget_bytes = ?, attachment_access_counter = ? WHERE id = 1",
                (cache_bookkeeping["attachment_cache_budget_bytes"], cache_bookkeeping["attachment_access_counter"]),
            )

        await db.commit()


async def _seed_settings(db) -> None:
    await db.execute("INSERT INTO settings (id, enabled_tools) VALUES (1, ?)", (json.dumps(DEFAULT_ENABLED_TOOLS),))


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
            "INSERT INTO model_configs (endpoint_id, model_name, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
