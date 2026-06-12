from __future__ import annotations

import json
import sqlite3

from .connection import get_db
from .schema import CREATE_TABLES_SQL
from .seeds import (
    DEFAULT_ENABLED_TOOLS,
    DEFAULT_SETTINGS,
    SEED_INTERACTIVE_FRAGMENTS,
    SEED_MOOD_FRAGMENTS,
    SEED_PHRASE_BANK,
)


def schema_safety_problems() -> list[str]:
    """Return why the live DB schema is unsafe for the preset engine, or ``[]`` if safe.

    Call right after ``run_pending`` so any developer schema change the preset engine
    cannot safely handle -- a new uncovered table, a stale policy constant, a migration
    that leaves a table unlike ``CREATE_TABLES_SQL`` (the 0026 persona_lock_id /
    0008 vestigial-column class of bug) -- surfaces at boot, naming the constant or
    migration to fix.

    Non-fatal by design: the check guards *preset/backup* operations, not normal app
    queries, so a schema quirk must warn loudly rather than brick the whole app at
    boot (a single missed cleanup migration would otherwise refuse every real
    install's startup). The preset ops themselves still call
    ``presets.assert_schema_safe`` and fail hard on the same problems, so no backup is
    ever built or applied against an unsafe schema.

    ``backend.presets`` is imported lazily here because it pulls in the migration
    runner and would otherwise close an import cycle through this package. ``DB_PATH``
    is read off the ``connection`` module at call time (not the import-time binding)
    so a monkeypatched path in tests resolves correctly.
    """
    from .. import presets
    from . import connection

    conn = sqlite3.connect(connection.DB_PATH)
    try:
        return presets.schema_safety_problems(conn)
    finally:
        conn.close()


async def init_db():
    """Create the latest schema for fresh installs and seed empty tables.

    Schema *evolution* (column adds, table renames, backfills) lives in
    ``backend/database/migrations/`` and is applied separately by
    ``run_pending`` after this function returns. Keep this file focused on
    fresh-install shape + seed data only.
    """
    async with get_db() as db:
        await db.executescript(CREATE_TABLES_SQL)

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM settings"))
        if row[0]["c"] == 0:
            await _seed_settings(db)

        ep_row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM endpoints"))
        if ep_row[0]["c"] == 0:
            s_rows = list(await db.execute_fetchall("SELECT * FROM settings WHERE id = 1"))
            if s_rows:
                await _seed_endpoint_from(db, dict(s_rows[0]))

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM mood_fragments"))
        if row[0]["c"] == 0:
            await _seed_mood_fragments(db)

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM interactive_fragments"))
        if row[0]["c"] == 0:
            await _seed_interactive_fragments(db)

        row = list(await db.execute_fetchall("SELECT COUNT(*) as c FROM phrase_bank"))
        if row[0]["c"] == 0:
            await _seed_phrase_bank(db)

        await db.commit()


async def reset_to_defaults() -> None:
    """Delete all user-modified data and re-seed tables to defaults.

    The settings row is rebuilt (DELETE + re-seed), but the two attachment-cache
    bookkeeping columns are carried across the rebuild rather than snapped back to
    their schema defaults. They describe the ``workflow_attachments`` rows, which
    reset RETAINS, so resetting them would desync the cache from the data it
    tracks -- and neither is a user-facing setting that "reset to defaults" is
    meant to clear:

      - ``attachment_access_counter`` is the monotonic LRU-3 clock. Resetting it
        to 0 while retained rows still hold ``recent_accesses`` from the old
        counter space would invert eviction order: old artifacts would look
        freshest (high counter) and survive, while genuinely new post-reset
        artifacts (low counter) would be evicted first.
      - ``attachment_cache_budget_bytes`` is the cache size limit; dropping a
        tuned budget back to the default would silently change eviction pressure
        on retained rows.
    """
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
        await db.execute("DELETE FROM model_configs")
        await db.execute("DELETE FROM endpoints")

        await _seed_settings(db)
        await _seed_endpoint_from(db, DEFAULT_SETTINGS)
        await _seed_mood_fragments(db)
        await _seed_interactive_fragments(db)
        await _seed_phrase_bank(db)

        if cache_bookkeeping is not None:
            await db.execute(
                "UPDATE settings SET attachment_cache_budget_bytes = ?, attachment_access_counter = ? WHERE id = 1",
                (
                    cache_bookkeeping["attachment_cache_budget_bytes"],
                    cache_bookkeeping["attachment_access_counter"],
                ),
            )

        await db.commit()


async def _seed_settings(db) -> None:
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


async def _seed_endpoint_from(db, s: dict) -> None:
    """Create an endpoint + writer/agent model_configs from a settings-shaped dict,
    then link both back-references on settings.id=1."""
    cur = await db.execute(
        "INSERT INTO endpoints (url, api_key) VALUES (?, ?)",
        (
            s.get("endpoint_url", "http://localhost:5000/v1"),
            s.get("api_key", ""),
        ),
    )
    endpoint_id = cur.lastrowid
    writer = await db.execute(
        "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'writer')",
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
    agent = await db.execute(
        "INSERT INTO model_configs (endpoint_id, model_name, system_prompt, temperature, min_p, top_k, top_p, repetition_penalty, max_tokens, role) VALUES (?, ?, '', ?, ?, ?, ?, ?, ?, 'agent')",
        (
            endpoint_id,
            s.get("model_name", "default"),
            s.get("temperature", 0.8),
            s.get("min_p", 0.0),
            s.get("top_k", 40),
            s.get("top_p", 0.95),
            s.get("repetition_penalty", 1.0),
            s.get("max_tokens", 4096),
        ),
    )
    await db.execute(
        "UPDATE endpoints SET active_model_config_id = ?, agent_active_model_config_id = ? WHERE id = ?",
        (writer.lastrowid, agent.lastrowid, endpoint_id),
    )
    await db.execute(
        "UPDATE settings SET active_endpoint_id = ? WHERE id = 1",
        (endpoint_id,),
    )


async def _seed_mood_fragments(db) -> None:
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


async def _seed_interactive_fragments(db) -> None:
    for df in SEED_INTERACTIVE_FRAGMENTS:
        await db.execute(
            "INSERT INTO interactive_fragments (id, label, description, field_type, required, enabled, injection_label, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                df["id"],
                df["label"],
                df["description"],
                df["field_type"],
                1 if df["required"] else 0,
                1 if df.get("enabled", True) else 0,
                df["injection_label"],
                df["sort_order"],
            ),
        )


async def _seed_phrase_bank(db) -> None:
    # A seed entry is either a raw regex pattern (str) or a list of literal
    # variant phrases.
    for entry in SEED_PHRASE_BANK:
        if isinstance(entry, str):
            await db.execute(
                "INSERT INTO phrase_bank (variants, kind, pattern) VALUES (?, 'regex', ?)",
                (json.dumps([]), entry),
            )
        else:
            await db.execute(
                "INSERT INTO phrase_bank (variants, kind, pattern) VALUES (?, 'literal', NULL)",
                (json.dumps(entry),),
            )
