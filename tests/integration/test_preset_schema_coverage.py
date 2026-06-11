"""Drift backstop + end-to-end exercise for the schema-driven preset engine.

The merge engine derives its mechanics from the live schema, so adding a table or
an FK column needs no edit in ``presets.py``. The price of that is a loud check that
nothing new escapes the *policy* declared in ``preset_schema.py``: every table must
belong to a domain (or be excluded), every FK must resolve, and no secret-looking
column may be unaccounted for. These tests are that check, plus a full round-trip
that drives the generic engine across every domain at once.
"""

from __future__ import annotations

import sqlite3

from backend import presets
from backend.database.schema import CREATE_TABLES_SQL


def _fresh_schema_db(tmp_path, extra_sql: str = "") -> sqlite3.Connection:
    """An in-memory-equivalent DB with the current fresh-install schema (+extras)."""
    conn = sqlite3.connect(str(tmp_path / "schema.db"))
    conn.executescript(CREATE_TABLES_SQL)
    if extra_sql:
        conn.executescript(extra_sql)
    return conn


# ── drift check ──────────────────────────────────────────────────────────────


def test_live_schema_is_fully_covered(tmp_path):
    """Every current table maps to a domain, every FK resolves, every secret column
    is declared. This is the test that fails the day someone adds a table or a
    sensitive column without updating preset_schema.py."""
    conn = _fresh_schema_db(tmp_path)
    try:
        assert presets.schema_coverage_problems(conn) == []
    finally:
        conn.close()


def test_every_nonexcluded_table_resolves_to_one_domain(tmp_path):
    conn = _fresh_schema_db(tmp_path)
    try:
        schema = presets._build_schema_model(conn)
        for name in schema.tables:
            assert schema.domain_of(name) is not None, name
        # The excluded set is exactly machinery -- never something with a domain.
        for excluded in presets.ps.EXCLUDED_TABLES:
            assert excluded not in schema.tables
    finally:
        conn.close()


def test_coverage_flags_a_rogue_root_table(tmp_path):
    """A new top-level table with no DOMAIN_ROOT entry must be reported, naming it."""
    conn = _fresh_schema_db(tmp_path, "CREATE TABLE widgets (id TEXT PRIMARY KEY, label TEXT NOT NULL);")
    try:
        problems = presets.schema_coverage_problems(conn)
        assert any("widgets" in p for p in problems), problems
    finally:
        conn.close()


def test_coverage_flags_an_undeclared_secret_column(tmp_path):
    conn = _fresh_schema_db(tmp_path, "ALTER TABLE settings ADD COLUMN refresh_token TEXT NOT NULL DEFAULT '';")
    try:
        problems = presets.schema_coverage_problems(conn)
        assert any("refresh_token" in p for p in problems), problems
    finally:
        conn.close()


def test_new_cascade_child_is_handled_with_zero_edits(tmp_path):
    """The whole point of the refactor: a brand-new child table hung off an existing
    entity via ON DELETE CASCADE is classified, domained, ordered and covered purely
    from the schema -- no edit to presets.py or preset_schema.py."""
    conn = _fresh_schema_db(
        tmp_path,
        "CREATE TABLE message_notes ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,"
        "  note TEXT NOT NULL"
        ");",
    )
    try:
        schema = presets._build_schema_model(conn)
        t = schema.tables["message_notes"]
        assert t.kind == "surrogate"  # autoincrement id -> reinsert with remap
        assert schema.domain_of("message_notes") == "chats"  # joins messages -> conversations
        assert schema.root_of("message_notes").name == "conversations"
        # ordered after its owner, and fully covered.
        assert schema.order.index("message_notes") > schema.order.index("messages")
        assert presets.schema_coverage_problems(conn) == []
    finally:
        conn.close()


# ── full round-trip across every domain ────────────────────────────────────────


def _insert_conv_tree(path: str, cid: str, persona_id: int | None) -> None:
    conn = sqlite3.connect(path)
    try:
        ts = "2024-01-01T00:00:00"
        conn.execute(
            "INSERT INTO conversations (id, title, created_at, persona_lock_id) VALUES (?, ?, ?, ?)",
            (cid, f"Chat {cid}", ts, persona_id),
        )
        m1 = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, turn_index, parent_id, created_at) "
            "VALUES (?, 'user', 'hello', 0, NULL, ?)",
            (cid, ts),
        ).lastrowid
        m2 = conn.execute(
            "INSERT INTO messages (conversation_id, role, content, turn_index, parent_id, created_at) "
            "VALUES (?, 'assistant', 'world', 1, ?, ?)",
            (cid, m1, ts),
        ).lastrowid
        conn.execute("UPDATE conversations SET active_leaf_id = ? WHERE id = ?", (m2, cid))
        conn.execute("INSERT INTO director_state (conversation_id, active_moods) VALUES (?, '[]')", (cid,))
        conn.commit()
    finally:
        conn.close()


def _signature(path: str) -> dict:
    """Canonical, surrogate-id-independent content of every data domain.

    Surrogate ids (messages, personas, …) are never compared directly; references
    to them are resolved to the parent's portable identity (a persona's name, a
    leaf message's content) so two databases that differ only by autoincrement
    renumbering produce the same signature.
    """
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    def q(sql):
        return sorted(tuple(r) for r in conn.execute(sql).fetchall())

    try:
        return {
            "characters": q(
                "SELECT cc.name, cc.world_id, up.name FROM character_cards cc "
                "LEFT JOIN user_personas up ON cc.persona_lock_id = up.id"
            ),
            "conversations": q("SELECT id, title FROM conversations"),
            "conv_persona": q(
                "SELECT c.id, up.name FROM conversations c " "LEFT JOIN user_personas up ON c.persona_lock_id = up.id"
            ),
            "messages": q("SELECT conversation_id, turn_index, role, content FROM messages"),
            "active_leaf": q("SELECT c.id, m.content FROM conversations c " "LEFT JOIN messages m ON c.active_leaf_id = m.id"),
            "director_state": q("SELECT conversation_id FROM director_state"),
            "worlds": q("SELECT id, name FROM worlds"),
            "lorebook_entries": q("SELECT world_id, name, content FROM lorebook_entries"),
            "personas": q("SELECT name, description FROM user_personas"),
            "phrase_bank": q("SELECT variants, kind, pattern FROM phrase_bank"),
            "fragments": q("SELECT id, label FROM mood_fragments"),
        }
    finally:
        conn.close()


async def test_full_round_trip_is_identity_modulo_surrogate_ids(client, db_path):
    """Seed every domain, export a full preset, scramble the live DB, then apply the
    file with replace=True: the database must come back row-for-row identical
    (ignoring autoincrement renumbering). One assertion exercises the entire generic
    engine -- topo order, surrogate remap, FK rewrite, self/cycle fixup, child-replace
    and cross-domain reconcile -- across all domains together."""
    path = str(db_path)

    # personas, worlds + entries, characters (one world-linked, one persona-locked).
    p1 = (await client.post("/api/user-personas", json={"name": "Ada"})).json()["id"]
    w1 = (await client.post("/api/worlds", json={"name": "Mythos"})).json()["id"]
    await client.post(f"/api/worlds/{w1}/entries", json={"name": "Lore A", "content": "alpha"})
    await client.post(f"/api/worlds/{w1}/entries", json={"name": "Lore B", "content": "beta"})
    linked = (await client.post("/api/characters", json={"name": "Linked"})).json()["id"]
    await client.put(f"/api/characters/{linked}", json={"world_id": w1})
    locked = (await client.post("/api/characters", json={"name": "Locked"})).json()["id"]
    await client.put(f"/api/characters/{locked}", json={"persona_lock_id": p1})

    # a chat tree, persona-locked, with an active leaf to remap.
    _insert_conv_tree(path, "conv-keep", p1)

    # configs touch + a phrase-bank row, to cover those domains too.
    await client.put("/api/settings", json={"user_name": "Ada", "api_key": "sk-keep"})

    before = _signature(path)

    name = (
        await client.post(
            "/api/presets/export",
            json={"domains": list(presets.ALL_DOMAINS), "strip_keys": False, "label": "roundtrip"},
        )
    ).json()["name"]
    preset_path = presets._library_path(name)

    # Scramble the live DB across domains: delete, edit, and add rows everywhere.
    await client.delete(f"/api/characters/{linked}")
    await client.put(f"/api/characters/{locked}", json={"name": "Renamed"})
    await client.post("/api/characters", json={"name": "Intruder"})
    _insert_conv_tree(path, "conv-extra", None)
    w2 = (await client.post("/api/worlds", json={"name": "Junk"})).json()["id"]
    await client.post(f"/api/worlds/{w2}/entries", json={"name": "noise", "content": "x"})
    await client.put("/api/settings", json={"user_name": "Eve"})

    # Drive the generic engine on a full-coverage file (replace = restore semantics).
    import asyncio

    summary = await asyncio.to_thread(presets.apply_preset, preset_path, replace=True)

    after = _signature(path)
    assert after == before, {k: (before[k], after[k]) for k in before if before[k] != after[k]}
    assert summary["chats"] == 1 and summary["characters"] == 2 and summary["configs"] == 1

    # And the committed state has no dangling foreign keys.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
