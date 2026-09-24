"""Upgrade coverage for migration 0067: progressive fragments and direction notes become state.

Builds a database as it stood before 0067 -- the frozen historical schema run
through every earlier migration -- fills it with legacy state across a branching
tree, runs 0067, and then holds the result to the same standalone check that is
meant to run against real databases (``scripts/check_state_conversion.py``).
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from pathlib import Path

from backend.core import fold_events
from backend.database.migrations import MIGRATIONS

_ROOT = Path(__file__).resolve().parents[2]
_BASELINE = _ROOT / "tests" / "fixtures" / "schema_pre_group_chats.sql"
_MIGRATION = "0067_state_fragments"
sys.path.insert(0, str(_ROOT / "scripts"))
check_state_conversion = importlib.import_module("check_state_conversion")

_OLD = "2026-01-01T00:00:00+00:00"


def _pre_0067(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(_BASELINE.read_text())
    conn.execute("CREATE TABLE schema_migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (datetime('now')))")
    for name in MIGRATIONS:
        if name >= _MIGRATION:
            break
        importlib.import_module(f"backend.database.migrations.{name}").migrate(conn)
        conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (name,))
        conn.commit()
    return conn


def _run_0067(conn: sqlite3.Connection) -> None:
    importlib.import_module(f"backend.database.migrations.{_MIGRATION}").migrate(conn)
    conn.execute("INSERT INTO schema_migrations (id) VALUES (?)", (_MIGRATION,))
    conn.commit()


def _settings(conn: sqlite3.Connection, *, record: int, inject: str, tools: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (id, endpoint_url, model_name, direction_notes_record, direction_notes_inject, "
        "enabled_tools) VALUES (1, 'http://x', 'm', ?, ?, ?)",
        (record, inject, json.dumps(tools)),
    )


def _fragment(conn: sqlite3.Connection, fid: str, field_type: str, timing: str = "post_turn", label: str = "") -> None:
    conn.execute(
        "INSERT INTO interactive_fragments (id, label, description, field_type, injection_label, direction_note_timing) "
        "VALUES (?, ?, 'd', ?, ?, ?)",
        (fid, label or fid.title(), field_type, (label or fid.title()) + " heading", timing),
    )


def _message(conn, cid: str, mid: int, parent: int | None, role: str, progressive: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO messages (id, conversation_id, role, content, turn_index, parent_id, progressive_fields, created_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
        (mid, cid, role, f"{role} {mid}", parent, json.dumps(progressive or {}), _OLD),
    )


def _note(conn, cid: str, mid: int, fid: str, label: str, content: str) -> None:
    conn.execute(
        "INSERT INTO direction_notes (conversation_id, message_id, interactive_fragment_id, interactive_fragment_label, "
        "content, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (cid, mid, fid, label, content, _OLD),
    )


def _legacy_tree(conn: sqlite3.Connection) -> None:
    """A tree with a regenerated branch, a partial save, a dropped value, and notes.

    1 greeting
    └ 2 user
      ├ 3 asst {trust: wary, mood: calm}           note(plan) "north"
      │ └ 4 user
      │   ├ 5 asst {trust: warming}  -- mood dropped
      │   │ └ 6 user                 human note "Plot" "the key is fake", human note "Note" "remember"
      │   │   └ 7 asst {}            -- partial save: keeps warming
      │   └ 8 asst {trust: wary, mood: tense}   -- a sibling branch
      └ 9 asst {trust: hostile}
    """
    conn.execute("INSERT INTO conversations (id, title, created_at) VALUES ('c1', 'Tree', ?)", (_OLD,))
    _message(conn, "c1", 1, None, "assistant")
    _message(conn, "c1", 2, 1, "user")
    _message(conn, "c1", 3, 2, "assistant", {"trust": "wary", "mood": "calm"})
    _message(conn, "c1", 4, 3, "user")
    _message(conn, "c1", 5, 4, "assistant", {"trust": "warming"})
    _message(conn, "c1", 6, 5, "user")
    _message(conn, "c1", 7, 6, "assistant", {})
    _message(conn, "c1", 8, 4, "assistant", {"trust": "wary", "mood": "tense"})
    _message(conn, "c1", 9, 2, "assistant", {"trust": "hostile"})
    _note(conn, "c1", 3, "plan", "Plan heading", "north")
    _note(conn, "c1", 6, "human", "Plot", "the key is fake")
    _note(conn, "c1", 6, "human", "Note", "remember")


def _folded(conn: sqlite3.Connection, leaf: int) -> dict[str, list[str]]:
    path, cursor = [], leaf
    while cursor is not None:
        path.append(cursor)
        cursor = conn.execute("SELECT parent_id FROM messages WHERE id = ?", (cursor,)).fetchone()[0]
    path.reverse()
    rank = {mid: i for i, mid in enumerate(path)}
    conn.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM fragment_state_events")]
    finally:
        conn.row_factory = None
    events = sorted((r for r in rows if r["message_id"] in rank), key=lambda r: (rank[r["message_id"]], r["id"]))
    view = fold_events(events)
    return {fid: [e.text for e in view.active(fid)] for fid in view.entries}


def test_converts_fragments_settings_and_every_branch_tip(tmp_path):
    db = tmp_path / "app.db"
    conn = _pre_0067(db)
    _settings(conn, record=1, inject="writer", tools={"direct_scene": True, "record_direction_note": True})
    _fragment(conn, "trust", "progressive")
    _fragment(conn, "mood", "progressive")
    _fragment(conn, "plan", "direction_note", timing="pre_writer")
    _fragment(conn, "arc", "direction_note")
    _legacy_tree(conn)
    conn.commit()

    _run_0067(conn)

    fragments = {
        row[0]: row[1:]
        for row in conn.execute(
            "SELECT id, field_type, state_mode, state_update, state_inject, enabled FROM interactive_fragments"
        )
    }
    assert fragments["trust"][:4] == ("state", "value", "before_writer", "both")
    assert fragments["plan"][:4] == ("state", "entries", "before_writer", "writer")
    assert fragments["arc"][:4] == ("state", "entries", "after_reply", "writer")
    # Human notes moved to an ordinary, enabled Notes fragment on the old inject target.
    assert fragments["notes"] == ("state", "entries", "manual", "writer", 1)
    # The two starters ship disabled.
    assert fragments["scene_continuity"][:4] == ("state", "value", "before_writer", "writer")
    assert fragments["open_threads"][:4] == ("state", "entries", "after_reply", "both")
    assert fragments["scene_continuity"][4] == fragments["open_threads"][4] == 0
    settings = conn.execute("SELECT state_updates, enabled_tools FROM settings").fetchone()
    assert settings[0] == 1
    assert json.loads(settings[1]) == {"direct_scene": True, "update_state": True}

    assert _folded(conn, 7) == {
        "trust": ["warming"],  # the partial save kept the value the old code lost
        "plan": ["north"],
        "notes": ["Plot: the key is fake", "remember"],
    }
    assert _folded(conn, 8) == {"trust": ["wary"], "mood": ["tense"], "plan": ["north"]}
    assert _folded(conn, 9) == {"trust": ["hostile"]}
    # Set retains the entry id; a value that disappeared was retired.
    ops = conn.execute("SELECT message_id, fragment_id, op FROM fragment_state_events ORDER BY id").fetchall()
    assert (5, "trust", "revise") in ops and (5, "mood", "retire") in ops
    retired = conn.execute("SELECT text FROM fragment_state_events WHERE message_id = 5 AND op = 'retire'").fetchone()
    assert retired == ("calm",)
    assert not any(message_id == 7 for message_id, _, _ in ops)
    conn.close()

    report = check_state_conversion.check(str(db))
    assert report.mismatches == []
    assert (report.leaves, report.checked, report.restored) == (3, 3, 1)


def test_recording_off_makes_converted_notes_manual_only_and_keeps_progressive_updating(tmp_path):
    conn = _pre_0067(tmp_path / "app.db")
    _settings(conn, record=0, inject="off", tools={"direct_scene": True})
    _fragment(conn, "trust", "progressive")
    _fragment(conn, "plan", "direction_note", timing="pre_writer")
    conn.commit()

    _run_0067(conn)

    rows = dict(conn.execute("SELECT id, state_update FROM interactive_fragments WHERE id IN ('trust', 'plan')").fetchall())
    assert rows == {"trust": "before_writer", "plan": "manual"}
    assert conn.execute("SELECT state_updates FROM settings").fetchone() == (1,)
    # No human notes: the Notes fragment ships disabled, like a fresh install's.
    assert conn.execute("SELECT enabled, state_inject FROM interactive_fragments WHERE id = 'notes'").fetchone() == (0, "both")


def test_a_chats_only_snapshot_converts_without_fragments(tmp_path):
    """Imported presets can hold one domain: chats here, with no fragment rows."""
    db = tmp_path / "chats.db"
    conn = _pre_0067(db)
    # Earlier migrations seed fragments; a chats-only preset carries none.
    conn.execute("DELETE FROM interactive_fragments")
    _settings(conn, record=1, inject="both", tools={})
    _legacy_tree(conn)
    conn.commit()

    _run_0067(conn)

    # Nothing was seeded into the absent fragments domain...
    assert conn.execute("SELECT COUNT(*) FROM interactive_fragments").fetchone() == (0,)
    # ...but the state converted, labelled by id where no fragment names it.
    assert _folded(conn, 7)["trust"] == ["warming"]
    labels = {row[0] for row in conn.execute("SELECT fragment_label FROM fragment_state_events WHERE fragment_id = 'trust'")}
    assert labels == {"trust"}
    conn.close()
    assert check_state_conversion.check(str(db)).mismatches == []


def test_a_notes_id_taken_by_another_fragment_moves_notes_aside(tmp_path):
    conn = _pre_0067(tmp_path / "app.db")
    _settings(conn, record=1, inject="both", tools={})
    _fragment(conn, "notes", "string", label="Scratch")
    conn.execute("INSERT INTO conversations (id, title, created_at) VALUES ('c1', 't', ?)", (_OLD,))
    _message(conn, "c1", 1, None, "assistant")
    _note(conn, "c1", 1, "human", "Note", "mine")
    conn.commit()

    _run_0067(conn)

    assert conn.execute("SELECT field_type FROM interactive_fragments WHERE id = 'notes'").fetchone() == ("string",)
    assert conn.execute("SELECT label, enabled FROM interactive_fragments WHERE id = 'user_notes'").fetchone() == (
        "Notes",
        1,
    )
    assert conn.execute("SELECT fragment_id, text, source FROM fragment_state_events").fetchall() == [
        ("user_notes", "mine", "user")
    ]


def test_check_script_reports_a_leaf_that_lost_state(tmp_path):
    db = tmp_path / "app.db"
    conn = _pre_0067(db)
    _settings(conn, record=1, inject="both", tools={})
    _legacy_tree(conn)
    conn.commit()
    _run_0067(conn)
    conn.execute("DELETE FROM fragment_state_events WHERE message_id = 9")
    conn.commit()
    conn.close()

    report = check_state_conversion.check(str(db))
    assert len(report.mismatches) == 1 and "leaf 9" in report.mismatches[0]
