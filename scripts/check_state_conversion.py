#!/usr/bin/env python3
"""Check that migration 0067 kept every branch tip's state.

For each leaf of each conversation, the state folded from ``fragment_state_events``
must equal the legacy state on its path:

* every progressive value of the most recent assistant snapshot on the path, as
  a one-entry fragment -- except where that snapshot is empty (a partial save or a
  turn without ``direct_scene``), in which case the conversion deliberately keeps
  the last non-empty snapshot instead; those leaves are counted as *restored*;
* every saved direction note on the path, in branch order, as entries of its
  fragment -- user-authored notes as entries of the Notes fragment, prefixed with
  their label unless it was the default "Note".

It also lists converted lists that start above the active-entry cap: they match,
but the Agent cannot add to them until enough entries are retired.

Run it on each real database before the cleanup migration drops the legacy
columns. It opens the database read-only. A leaf whose path gained messages after
the migration ran, and events written after it, are left out: they are new state,
with no legacy counterpart.

    .venv/bin/python scripts/check_state_conversion.py backend/data/app.db [--notes-id notes] [-v]

Exits 1 when any leaf differs, 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core import MAX_ACTIVE_ENTRIES, fold_events  # noqa: E402

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


def check(path: str, *, notes_id: str | None = None, verbose: bool = False) -> Report:
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
            elif verbose:
                print(f"ok  leaf {leaf_id}: {folded!r}")
    finally:
        conn.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("database", help="path to an Orb database migrated through 0067")
    parser.add_argument("--notes-id", help="fragment id user-authored notes moved to (detected when omitted)")
    parser.add_argument("-v", "--verbose", action="store_true", help="print every matching leaf too")
    args = parser.parse_args()
    report = check(args.database, notes_id=args.notes_id, verbose=args.verbose)
    for line in report.mismatches:
        print(f"MISMATCH {line}")
    for line in report.over_cap:
        print(f"OVER CAP {line}")
    print(
        f"{report.leaves} leaves: {report.checked} checked, {report.skipped_new} newer than the migration, "
        f"{report.restored} restored past an empty snapshot, {len(report.mismatches)} mismatched, "
        f"{len(report.over_cap)} fragment(s) over the {MAX_ACTIVE_ENTRIES}-entry cap"
    )
    return 1 if report.mismatches else 0


if __name__ == "__main__":
    raise SystemExit(main())
