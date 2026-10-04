"""Read-only oracle for migration 0067 state at each historical branch tip.

Compare folded events with the latest non-empty progressive snapshot and ordered direction notes, retaining user labels. Report
lists above the cap. Exclude messages/events added after migration because they have no legacy source.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from backend.core import MAX_ACTIVE_ENTRIES, fold_events

_MIGRATION = "0067_state_fragments"
_HUMAN = "human"
_DEFAULT_NOTE_LABEL = "Note"


@dataclass
class Report:
    leaves: int = 0
    checked: int = 0
    skipped_new: int = 0
    restored: int = 0
    mismatches: list[str] = field(default_factory=list)
    # Branch tips whose converted list starts above the active-entry cap, which
    # refuses every Agent add until enough entries are retired. Not a mismatch.
    over_cap: list[str] = field(default_factory=list)


def _when(value: str | None) -> datetime | None:
    """Parse both SQLite's ``datetime('now')`` and Orb's ISO timestamps as UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace(" ", "T"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value).strip()
    return str(value).strip()


def _snapshot(raw: object) -> dict[str, str]:
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        decoded = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): text for key, value in decoded.items() if (text := _text(value))}


def _notes_fragment_id(conn: sqlite3.Connection, override: str | None) -> str:
    if override:
        return override
    rows = conn.execute(
        "SELECT id FROM interactive_fragments WHERE field_type = 'state' AND state_update = 'manual' AND label = 'Notes' "
        "ORDER BY CASE id WHEN 'notes' THEN 0 ELSE 1 END, id"
    ).fetchall()
    return rows[0][0] if rows else "notes"


def check(path: str, *, notes_id: str | None = None) -> Report:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    report = Report()
    try:
        applied = conn.execute("SELECT applied_at FROM schema_migrations WHERE id = ?", (_MIGRATION,)).fetchone()
        if applied is None:
            raise SystemExit(f"{path}: migration {_MIGRATION} has not run on this database")
        migrated_at = _when(applied["applied_at"])
        assert migrated_at is not None
        notes_fid = _notes_fragment_id(conn, notes_id)

        messages = {row["id"]: dict(row) for row in conn.execute("SELECT * FROM messages")}
        children = {row["parent_id"] for row in messages.values() if row["parent_id"] is not None}
        notes_by_message: dict[int, list[dict]] = {}
        for row in conn.execute("SELECT * FROM direction_notes ORDER BY id"):
            notes_by_message.setdefault(row["message_id"], []).append(dict(row))
        events_by_message: dict[int, list[dict]] = {}
        for row in conn.execute("SELECT * FROM fragment_state_events ORDER BY id"):
            events_by_message.setdefault(row["message_id"], []).append(dict(row))

        for leaf_id in sorted(set(messages) - children):
            report.leaves += 1
            path: list[dict] = []
            cursor: int | None = leaf_id
            while cursor is not None and cursor in messages:
                path.append(messages[cursor])
                cursor = messages[cursor]["parent_id"]
            path.reverse()
            if any((_when(m["created_at"]) or migrated_at) > migrated_at for m in path):
                report.skipped_new += 1
                continue
            report.checked += 1

            expected: dict[str, list[str]] = {}
            assistant = [m for m in path if m["role"] == "assistant"]
            latest = _snapshot(assistant[-1]["progressive_fields"]) if assistant else {}
            nonempty = next((snap for m in reversed(assistant) if (snap := _snapshot(m["progressive_fields"]))), {})
            if assistant and not latest and nonempty:
                report.restored += 1
            for fid, value in nonempty.items():
                expected[fid] = [value]
            for message in path:
                for note in notes_by_message.get(message["id"], []):
                    content = (note["content"] or "").strip()
                    if not content:
                        continue
                    if note["interactive_fragment_id"] == _HUMAN:
                        label = (note["interactive_fragment_label"] or "").strip()
                        text = f"{label}: {content}" if label and label != _DEFAULT_NOTE_LABEL else content
                        expected.setdefault(notes_fid, []).append(text)
                    else:
                        expected.setdefault(note["interactive_fragment_id"], []).append(content)

            events = [
                event
                for message in path
                for event in events_by_message.get(message["id"], [])
                if (_when(event["created_at"]) or migrated_at) <= migrated_at
            ]
            view = fold_events(events)
            folded = {fid: [entry.text for entry in view.active(fid)] for fid in view.entries}
            for fid, texts in folded.items():
                if len(texts) > MAX_ACTIVE_ENTRIES:
                    report.over_cap.append(
                        f"conversation {path[0]['conversation_id']} leaf {leaf_id}: {fid} holds {len(texts)} entries"
                    )
            if folded != expected:
                report.mismatches.append(
                    f"conversation {path[0]['conversation_id']} leaf {leaf_id}: expected {expected!r}, folded {folded!r}"
                )
    finally:
        conn.close()
    return report
