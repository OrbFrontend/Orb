"""Migrate legacy progressive and direction-note data to state fragments.

Preserves branch state on imported databases with partial domains. Legacy
direction-note storage remains for a follow-up cleanup migration.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime

from ..schema import table_create_sql
from ..seeds import NOTES_STATE_FRAGMENT, STARTER_STATE_FRAGMENTS

_FRAGMENT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("state_mode", "TEXT DEFAULT NULL CHECK (state_mode IS NULL OR state_mode IN ('value', 'entries'))"),
    (
        "state_update",
        "TEXT DEFAULT NULL CHECK (state_update IS NULL OR state_update IN ('after_reply', 'before_writer', 'manual'))",
    ),
    (
        "state_inject",
        "TEXT DEFAULT NULL CHECK (state_inject IS NULL OR state_inject IN ('off', 'director', 'writer', 'both'))",
    ),
)
_SETTINGS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("state_updates", "INTEGER NOT NULL DEFAULT 1 CHECK (state_updates IN (0, 1))"),
)
_LOG_COLUMNS: tuple[tuple[str, str], ...] = (("state_report", "TEXT NOT NULL DEFAULT '{}'"),)

_OLD_TOOL = "record_direction_note"
_NEW_TOOL = "update_state"
_HUMAN_NOTE_ID = "human"
_DEFAULT_NOTE_LABEL = "Note"
_TIMING_TO_UPDATE = {"pre_writer": "before_writer", "post_turn": "after_reply"}
_INJECTS = ("off", "director", "writer", "both")


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # nosec B608 -- literal names


def _add_missing(conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> int:
    existing = _columns(conn, table)
    if not existing:
        return 0
    added = 0
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")  # nosec B608 -- names/DDL are module literals
            added += 1
    return added


def _legacy_settings(conn: sqlite3.Connection) -> tuple[bool, str]:
    """``(direction_notes_record, direction_notes_inject)``, defaulting when absent."""
    columns = _columns(conn, "settings")
    if not {"direction_notes_record", "direction_notes_inject"} <= columns:
        return False, "off"
    row = conn.execute("SELECT direction_notes_record, direction_notes_inject FROM settings WHERE id = 1").fetchone()
    if row is None:
        return False, "off"
    inject = row[1] if row[1] in _INJECTS else "off"
    return bool(row[0]), inject


def _convert_fragments(conn: sqlite3.Connection, record: bool, inject: str) -> int:
    columns = _columns(conn, "interactive_fragments")
    if not columns:
        return 0
    timing_column = "direction_note_timing" if "direction_note_timing" in columns else "'post_turn'"
    rows = conn.execute(
        f"SELECT id, field_type, {timing_column} FROM interactive_fragments "  # nosec B608 -- literal column or constant
        "WHERE field_type IN ('progressive', 'direction_note')"
    ).fetchall()
    for fid, field_type, timing in rows:
        if field_type == "progressive":
            settings = ("value", "before_writer", "both")
        else:
            update = _TIMING_TO_UPDATE.get(timing or "post_turn", "after_reply") if record else "manual"
            settings = ("entries", update, inject)
        conn.execute(
            "UPDATE interactive_fragments SET field_type = 'state', state_mode = ?, state_update = ?, state_inject = ? "
            "WHERE id = ?",
            (*settings, fid),
        )
    return len(rows)


def _insert_fragment(conn: sqlite3.Connection, row: dict) -> None:
    columns = [
        "id",
        "label",
        "description",
        "field_type",
        "required",
        "enabled",
        "injection_label",
        "sort_order",
        "state_mode",
        "state_update",
        "state_inject",
    ]
    values = [int(row[c]) if isinstance(row.get(c), bool) else row.get(c) for c in columns]
    conn.execute(
        f"INSERT INTO interactive_fragments ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",  # nosec B608 -- literal columns
        values,
    )


def _has_fragment_rows(conn: sqlite3.Connection) -> bool:
    """Skip seed rows in presets whose fragments table is empty."""
    return bool(_columns(conn, "interactive_fragments")) and bool(
        conn.execute("SELECT 1 FROM interactive_fragments LIMIT 1").fetchone()
    )


def _notes_fragment_id(conn: sqlite3.Connection) -> str:
    """The id user-authored notes move to: ``notes`` unless another fragment owns it."""
    if not _columns(conn, "interactive_fragments"):
        return NOTES_STATE_FRAGMENT["id"]
    taken = {row[0]: row[1] for row in conn.execute("SELECT id, field_type FROM interactive_fragments").fetchall()}
    candidate, n = NOTES_STATE_FRAGMENT["id"], 1
    while candidate in taken and not (candidate == NOTES_STATE_FRAGMENT["id"] and taken[candidate] == "state"):
        n += 1
        candidate = f"user_notes_{n}" if n > 2 else "user_notes"
    return candidate


def _seed_notes_fragment(conn: sqlite3.Connection, fid: str, *, has_human_notes: bool, inject: str) -> bool:
    if not _has_fragment_rows(conn) or conn.execute("SELECT 1 FROM interactive_fragments WHERE id = ?", (fid,)).fetchone():
        return False
    sort_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM interactive_fragments").fetchone()[0]
    # Preserve the old injection target, and enable the seed only when notes exist.
    _insert_fragment(
        conn,
        {
            **NOTES_STATE_FRAGMENT,
            "id": fid,
            "enabled": has_human_notes,
            "state_inject": inject if has_human_notes else NOTES_STATE_FRAGMENT["state_inject"],
            "sort_order": sort_order,
        },
    )
    return True


def _seed_starters(conn: sqlite3.Connection) -> int:
    if not _has_fragment_rows(conn):
        return 0
    seeded = 0
    for row in STARTER_STATE_FRAGMENTS:
        if conn.execute("SELECT 1 FROM interactive_fragments WHERE id = ?", (row["id"],)).fetchone():
            continue
        sort_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM interactive_fragments").fetchone()[0]
        _insert_fragment(conn, {**row, "sort_order": sort_order})
        seeded += 1
    return seeded


def _fragment_labels(conn: sqlite3.Connection) -> dict[str, str]:
    if not _columns(conn, "interactive_fragments"):
        return {}
    return {row[0]: (row[1] or row[0]) for row in conn.execute("SELECT id, label FROM interactive_fragments").fetchall()}


def _insert_event(conn: sqlite3.Connection, event: tuple) -> None:
    conn.execute(
        "INSERT INTO fragment_state_events "
        "(conversation_id, message_id, fragment_id, entry_id, op, text, mode, fragment_label, source, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        event,
    )


def _as_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value).strip()
    return str(value).strip()


def _convert_progressive(conn: sqlite3.Connection, labels: dict[str, str], now: str) -> int:
    """Turn per-message progressive snapshots into set/clear events along the tree."""
    if "progressive_fields" not in _columns(conn, "messages"):
        return 0
    # Parent messages always precede their children in id order.
    rows = conn.execute(
        "SELECT id, conversation_id, parent_id, role, progressive_fields, created_at FROM messages ORDER BY id"
    ).fetchall()
    state_after: dict[int, dict[str, tuple[str, str]]] = {}
    written = 0
    for mid, cid, parent_id, role, raw, created_at in rows:
        prior = state_after.get(parent_id, {}) if parent_id is not None else {}
        snapshot: dict = {}
        if role == "assistant" and isinstance(raw, str) and raw.strip():
            try:
                decoded = json.loads(raw)
            except ValueError:
                decoded = {}
            snapshot = decoded if isinstance(decoded, dict) else {}
        values = {str(key): text for key, value in snapshot.items() if (text := _as_text(value))}
        if not values:
            # Empty snapshots also represent partial saves, so they cannot clear prior state.
            state_after[mid] = prior
            continue
        current = dict(prior)
        for fid, text in values.items():
            label = labels.get(fid, fid)
            held = current.get(fid)
            if held is None:
                entry_id = uuid.uuid4().hex[:12]
                _insert_event(conn, (cid, mid, fid, entry_id, "add", text, "value", label, "agent", created_at or now))
                current[fid] = (entry_id, text)
                written += 1
            elif held[1] != text:
                _insert_event(conn, (cid, mid, fid, held[0], "revise", text, "value", label, "agent", created_at or now))
                current[fid] = (held[0], text)
                written += 1
        for fid in [fid for fid in current if fid not in values]:
            entry_id, gone = current.pop(fid)
            _insert_event(
                conn, (cid, mid, fid, entry_id, "retire", gone, "value", labels.get(fid, fid), "agent", created_at or now)
            )
            written += 1
        state_after[mid] = current
    return written


def _convert_direction_notes(conn: sqlite3.Connection, notes_fid: str, now: str) -> int:
    if not _columns(conn, "direction_notes"):
        return 0
    rows = conn.execute(
        "SELECT conversation_id, message_id, interactive_fragment_id, interactive_fragment_label, content, created_at "
        "FROM direction_notes ORDER BY id"
    ).fetchall()
    for cid, mid, fid, label, content, created_at in rows:
        text = (content or "").strip()
        if not text:
            continue
        if fid == _HUMAN_NOTE_ID:
            note_label = (label or "").strip()
            if note_label and note_label != _DEFAULT_NOTE_LABEL:
                text = f"{note_label}: {text}"
            event = (cid, mid, notes_fid, uuid.uuid4().hex[:12], "add", text, "entries", "Notes", "user", created_at or now)
        else:
            event = (cid, mid, fid, uuid.uuid4().hex[:12], "add", text, "entries", label or fid, "agent", created_at or now)
        _insert_event(conn, event)
    return len(rows)


def _rename_tool_key(conn: sqlite3.Connection) -> bool:
    if "enabled_tools" not in _columns(conn, "settings"):
        return False
    row = conn.execute("SELECT enabled_tools FROM settings WHERE id = 1").fetchone()
    if row is None or not row[0]:
        return False
    try:
        tools = json.loads(row[0])
    except ValueError:
        return False
    if not isinstance(tools, dict) or _OLD_TOOL not in tools:
        return False
    renamed = {(_NEW_TOOL if key == _OLD_TOOL else key): value for key, value in tools.items()}
    conn.execute("UPDATE settings SET enabled_tools = ? WHERE id = 1", (json.dumps(renamed),))
    return True


def _has_human_notes(conn: sqlite3.Connection) -> bool:
    return bool(_columns(conn, "direction_notes")) and bool(
        conn.execute("SELECT 1 FROM direction_notes WHERE interactive_fragment_id = ? LIMIT 1", (_HUMAN_NOTE_ID,)).fetchone()
    )


def migrate(conn: sqlite3.Connection) -> None:
    added = (
        _add_missing(conn, "interactive_fragments", _FRAGMENT_COLUMNS)
        + _add_missing(conn, "settings", _SETTINGS_COLUMNS)
        + _add_missing(conn, "conversation_logs", _LOG_COLUMNS)
    )
    has_messages = bool(_columns(conn, "messages"))
    if has_messages:
        conn.execute(table_create_sql("fragment_state_events"))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_state_event_message ON fragment_state_events(message_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_state_event_conversation ON fragment_state_events(conversation_id, fragment_id)"
        )

    record, inject = _legacy_settings(conn)
    converted = _convert_fragments(conn, record, inject)
    # Read labels after conversion so snapshots name each fragment as the user
    # last saw it; a value whose fragment is gone keeps its id as its label.
    labels = _fragment_labels(conn)
    notes_fid = _notes_fragment_id(conn)
    human = _has_human_notes(conn)
    seeded_notes = _seed_notes_fragment(conn, notes_fid, has_human_notes=human, inject=inject)
    seeded_starters = _seed_starters(conn)

    now = datetime.now(UTC).isoformat()
    progressive_events = notes = 0
    if has_messages:
        progressive_events = _convert_progressive(conn, labels, now)
        notes = _convert_direction_notes(conn, notes_fid, now)
    renamed = _rename_tool_key(conn)
    conn.commit()
    print(
        f"[migrations] 0067: added {added} column(s); converted {converted} fragment(s); "
        f"{progressive_events} progressive event(s); {notes} direction note(s); "
        f"notes fragment={notes_fid if seeded_notes else 'unchanged'}; {seeded_starters} starter(s); "
        f"tool key renamed={renamed}"
    )
