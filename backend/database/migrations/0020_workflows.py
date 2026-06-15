"""
0020_workflows -- schema + data migration for the workflows
feature.

Carries a 0019 -> final-shape delta and runs correctly on two entry shapes:
  - Fresh install: schema.py already creates the final shape, so every guarded
    ADD/CREATE below no-ops and only the legacy-source ports + drops do work.
  - Pre-workflow upgrade (existing main-shaped DB): the guarded ADDs fire and
    real user data is ported.

Legacy data ports (mandatory -- a real upgrade carries this data; skipping it
would silently drop user attachments and TTS configuration):
  - message_attachments rows -> user_attachments. Every pre-split row is a user
    upload, so all rows copy. message_attachments is then dropped. It is kept in
    schema.py's base schema because migration 0002 deletes from it on fresh
    install before any table-creating migration runs; removing that base
    definition crashes a fresh boot.
  - settings.tts_enabled / tts_auto_speak / tts_volume columns and the
    voice_profiles table -> settings.workflow_config["tts"] = {auto_play, volume}
    plus, per voice profile, character_cards.workflow_state["tts"] (the
    endpoint_id field is dropped -- no runtime consumer reads it). The legacy
    columns and table are then dropped.

Idempotent: every column ADD is PRAGMA-guarded, every table CREATE/DROP is
existence-guarded, and the TTS port short-circuits once the legacy sources are
gone, so a re-run (or a run on an already-final DB) is a no-op.
"""

from __future__ import annotations

import json
import sqlite3


def migrate(conn: sqlite3.Connection) -> None:
    _add_workflow_state_columns(conn)
    _add_settings_columns(conn)
    _split_attachments(conn)
    _port_tts(conn)
    conn.commit()


def _add_workflow_state_columns(conn: sqlite3.Connection) -> None:
    """Add a per-scope JSON workflow_state column to conversations, messages, and character_cards."""
    for table in ("conversations", "messages", "character_cards"):
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if "workflow_state" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN workflow_state TEXT DEFAULT NULL")
            print(f"[migrations] 0020: added workflow_state column to {table}")


def _add_settings_columns(conn: sqlite3.Connection) -> None:
    """Add settings.workflow_config plus the attachment-cache budget and access counter."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    if "workflow_config" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN workflow_config TEXT NOT NULL DEFAULT '{}'")
        print("[migrations] 0020: added workflow_config column to settings")
    if "attachment_cache_budget_bytes" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN attachment_cache_budget_bytes INTEGER NOT NULL DEFAULT 524288000")
        print("[migrations] 0020: added settings.attachment_cache_budget_bytes")
    if "attachment_access_counter" not in cols:
        conn.execute("ALTER TABLE settings ADD COLUMN attachment_access_counter INTEGER NOT NULL DEFAULT 0")
        print("[migrations] 0020: added settings.attachment_access_counter")


def _split_attachments(conn: sqlite3.Connection) -> None:
    """Split message_attachments into user_attachments + workflow_attachments.

    message_attachments may carry a `source` column on a dev DB that was migrated
    incrementally; when present, only rows with source 'user' (or NULL) are user
    uploads and copy. On any DB entering at the 0019 boundary there is no `source`
    column and every row is a user upload, so all rows copy. The `source in cols`
    branch exists solely to handle the former case; `source` is not part of the
    final schema and is never added here.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}

    if "user_attachments" not in tables:
        conn.execute(
            """
            CREATE TABLE user_attachments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
                mime_type TEXT NOT NULL,
                data_b64 TEXT NOT NULL,
                filename TEXT,
                size INTEGER,
                created_at TEXT NOT NULL
            )
            """
        )
        print("[migrations] 0020: created user_attachments")

    if "workflow_attachments" not in tables:
        conn.execute(
            """
            CREATE TABLE workflow_attachments (
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
            )
            """
        )
        print("[migrations] 0020: created workflow_attachments")

    if "message_attachments" in tables:
        ma_cols = {row[1] for row in conn.execute("PRAGMA table_info(message_attachments)").fetchall()}
        if "source" in ma_cols:
            conn.execute(
                """
                INSERT INTO user_attachments
                    (id, message_id, mime_type, data_b64, filename, size, created_at)
                SELECT id, message_id, mime_type, data_b64, filename, size, created_at
                FROM message_attachments
                WHERE (source = 'user' OR source IS NULL)
                  AND message_id IN (SELECT id FROM messages)
                """
            )
        else:
            conn.execute(
                """
                INSERT INTO user_attachments
                    (id, message_id, mime_type, data_b64, filename, size, created_at)
                SELECT id, message_id, mime_type, data_b64, filename, size, created_at
                FROM message_attachments
                WHERE message_id IN (SELECT id FROM messages)
                """
            )
        conn.execute("DROP TABLE message_attachments")
        print("[migrations] 0020: dropped message_attachments after user-row copy")


def _port_tts(conn: sqlite3.Connection) -> None:
    """Port legacy TTS storage into workflow_config + per-card state.

    Reads the legacy settings.tts_* columns and voice_profiles rows, writes the
    final runtime shape directly, then drops the legacy storage. Gated on the
    legacy sources still being present, so it is a no-op once they are gone.
    """
    settings_cols = {row[1] for row in conn.execute("PRAGMA table_info(settings)").fetchall()}
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    legacy_cols = [c for c in ("tts_enabled", "tts_auto_speak", "tts_volume") if c in settings_cols]
    has_voice_table = "voice_profiles" in tables

    if not legacy_cols and not has_voice_table:
        return

    select = "SELECT workflow_config" + ("".join(f", {c}" for c in legacy_cols)) + " FROM settings WHERE id = 1"
    row = conn.execute(select).fetchone()
    if row is not None:
        try:
            workflow_config = json.loads(row[0] or "{}")
        except (TypeError, json.JSONDecodeError):
            workflow_config = {}
        legacy = dict(zip(legacy_cols, row[1:], strict=True))

        if has_voice_table:
            cc_cols = {r[1] for r in conn.execute("PRAGMA table_info(character_cards)").fetchall()}
            if "workflow_state" in cc_cols:
                _move_voice_profiles(conn)

        volume = legacy.get("tts_volume")
        workflow_config["tts"] = {
            "auto_play": bool(legacy.get("tts_auto_speak")),
            "volume": float(volume) if isinstance(volume, (int, float)) else 0.75,
        }
        conn.execute("UPDATE settings SET workflow_config = ? WHERE id = 1", (json.dumps(workflow_config),))
        print("[migrations] 0020: ported TTS config to {auto_play, volume}")

    for col in legacy_cols:
        conn.execute(f"ALTER TABLE settings DROP COLUMN {col}")
        print(f"[migrations] 0020: dropped settings.{col}")
    if has_voice_table:
        conn.execute("DROP TABLE voice_profiles")
        print("[migrations] 0020: dropped voice_profiles table")


def _move_voice_profiles(conn: sqlite3.Connection) -> None:
    """Move each voice_profiles row into its card's workflow_state["tts"] slot.

    A card whose slot is unparseable or already carries a tts profile (set via
    the config panel) is left alone -- the live value wins. The vestigial
    endpoint_id field is not carried over.
    """
    for vp in conn.execute(
        """
        SELECT character_card_id, backend, voice_id, language, rate, pitch,
               enabled, api_url, api_key, model
        FROM voice_profiles
        """
    ).fetchall():
        card_id = vp[0]
        cc_row = conn.execute("SELECT workflow_state FROM character_cards WHERE id = ?", (card_id,)).fetchone()
        if cc_row is None:
            continue
        try:
            cc_state = json.loads(cc_row[0]) if cc_row[0] else {}
        except (TypeError, json.JSONDecodeError):
            cc_state = {}
        if not isinstance(cc_state, dict) or "tts" in cc_state:
            continue
        cc_state["tts"] = {
            "backend": vp[1],
            "voice_id": vp[2],
            "language": vp[3],
            "rate": vp[4],
            "pitch": vp[5],
            "enabled": bool(vp[6]),
            "api_url": vp[7] or "",
            "api_key": vp[8] or "",
            "model": vp[9] or "",
        }
        conn.execute("UPDATE character_cards SET workflow_state = ? WHERE id = ?", (json.dumps(cc_state), card_id))
