"""Migration 0062: keep existing Spark-TTS sidecar profiles working.

The rename is the part that can break a working setup. `spark` used to mean
"the OrbTTS sidecar at http://localhost:9300"; it now means the built-in
cloner, which needs a downloaded model and an enrolled voice. A profile written
before this release names a server the user installed and may still be running,
so those move to `spark_remote` -- the same adapter under its new name -- rather
than being repointed at a model that is probably not on disk.

"""

from __future__ import annotations

import importlib
import json
import sqlite3

from backend.workflows.tts.engine.router import get_adapter


def _migrate(conn: sqlite3.Connection) -> None:
    importlib.import_module("backend.database.migrations.0062_spark_tts_backend").migrate(conn)


def _staged() -> sqlite3.Connection:
    """A pre-0062 database holding one profile of each kind."""
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE character_cards (id TEXT PRIMARY KEY, workflow_state TEXT)")
    conn.execute("CREATE TABLE group_members (id TEXT PRIMARY KEY, workflow_state TEXT)")
    conn.execute("CREATE TABLE workflow_attachments (id INTEGER PRIMARY KEY, workflow_id TEXT, generation_metadata TEXT)")
    conn.executemany(
        "INSERT INTO character_cards VALUES (?, ?)",
        [
            (
                "sidecar",
                json.dumps({"tts": {"backend": "spark", "voice_id": "spark_female_warm", "api_url": "http://localhost:9300"}}),
            ),
            ("edge", json.dumps({"tts": {"backend": "edge", "voice_id": "en-US-JennyNeural"}})),
            ("no-workflows", None),
            ("not-json", "}{ not json"),
            ("other-workflow", json.dumps({"image_gen": {"model": "x"}})),
        ],
    )
    conn.execute("INSERT INTO group_members VALUES (?, ?)", ("m1", json.dumps({"tts": {"backend": "spark"}})))
    conn.executemany(
        "INSERT INTO workflow_attachments VALUES (?, ?, ?)",
        [
            (1, "tts", json.dumps({"backend": "spark", "text": "hello"})),
            (2, "tts", json.dumps({"backend": "edge", "text": "hello"})),
            (3, "image_gen", json.dumps({"backend": "spark"})),  # a different workflow's record
        ],
    )
    return conn


def _tts(conn: sqlite3.Connection, card_id: str) -> dict:
    raw = conn.execute("SELECT workflow_state FROM character_cards WHERE id = ?", (card_id,)).fetchone()[0]
    return json.loads(raw)["tts"]


def test_moves_existing_spark_profiles_to_the_sidecar_backend():
    conn = _staged()
    _migrate(conn)
    moved = _tts(conn, "sidecar")
    assert moved["backend"] == "spark_remote"
    # The rest of the profile is untouched: the sidecar still wants its url and
    # its preset, and the point of the rename is that it keeps working.
    assert moved["voice_id"] == "spark_female_warm"
    assert moved["api_url"] == "http://localhost:9300"
    # And the name it moved to is a backend that actually resolves.
    assert get_adapter(moved["backend"]) is not None


def test_moves_group_member_and_attachment_records_too():
    conn = _staged()
    _migrate(conn)
    member = json.loads(conn.execute("SELECT workflow_state FROM group_members WHERE id='m1'").fetchone()[0])
    assert member["tts"]["backend"] == "spark_remote"
    stored = json.loads(conn.execute("SELECT generation_metadata FROM workflow_attachments WHERE id=1").fetchone()[0])
    assert stored["backend"] == "spark_remote"
    assert stored["text"] == "hello"


def test_leaves_everything_else_alone():
    conn = _staged()
    _migrate(conn)
    assert _tts(conn, "edge")["backend"] == "edge"
    assert conn.execute("SELECT workflow_state FROM character_cards WHERE id='no-workflows'").fetchone()[0] is None
    assert conn.execute("SELECT workflow_state FROM character_cards WHERE id='not-json'").fetchone()[0] == "}{ not json"
    assert "tts" not in json.loads(
        conn.execute("SELECT workflow_state FROM character_cards WHERE id='other-workflow'").fetchone()[0]
    )
    # A non-TTS workflow's record that happens to say "spark" is not ours.
    assert (
        json.loads(conn.execute("SELECT generation_metadata FROM workflow_attachments WHERE id=3").fetchone()[0])["backend"]
        == "spark"
    )


def test_is_idempotent():
    """The runner records applied migrations, but a restore or a hand-run must
    not double-apply -- and a second pass here must match nothing at all."""
    conn = _staged()
    _migrate(conn)
    first = _tts(conn, "sidecar")
    _migrate(conn)
    assert _tts(conn, "sidecar") == first


def test_survives_a_database_that_predates_the_tables():
    conn = sqlite3.connect(":memory:")
    _migrate(conn)
