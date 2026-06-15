"""Drift backstop + end-to-end exercise for the schema-driven preset engine.

The merge engine derives its mechanics from the live schema, so adding a table or
an FK column needs no edit in ``presets.py``. The price of that is a loud check that
nothing new escapes the *policy* declared in ``preset_schema.py``: every table must
belong to a domain (or be excluded), every FK must resolve, and no secret-looking
column may be unaccounted for. These tests are that check, plus a full round-trip
that drives the generic engine across every domain at once.
"""

from __future__ import annotations

import importlib
import sqlite3

import pytest

from backend import presets
from backend.database import schema
from backend.database.schema import CREATE_TABLES_SQL

_mig_0027 = importlib.import_module("backend.database.migrations.0027_rebuild_persona_lock_fks")


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


def test_coverage_flags_a_not_null_deferred_edge(tmp_path):
    """A deferred FK edge (self ref, or a crossref broken to break a cycle) is
    inserted NULL during the merge, so a NOT NULL one would fail every import. The
    coverage check must surface that the moment such a column is added."""
    conn = _fresh_schema_db(
        tmp_path,
        "CREATE TABLE tree_nodes ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,"
        "  parent_id INTEGER NOT NULL REFERENCES tree_nodes(id) ON DELETE CASCADE"
        ");",
    )
    try:
        schema = presets._build_schema_model(conn)
        assert ("tree_nodes", "parent_id") in schema.deferred  # a self edge -> deferred
        problems = presets.schema_coverage_problems(conn)
        assert any("tree_nodes.parent_id" in p and "NOT NULL" in p for p in problems), problems
    finally:
        conn.close()


def test_domain_list_is_frozen():
    """Domains are baked into every exported file's meta, so renaming one silently
    breaks import for every preset already out there. This frozen literal turns a
    rename into a CI failure; *adding* a domain is a deliberate one-line edit here
    (append only)."""
    assert presets.ALL_DOMAINS == ["characters", "chats", "configs", "fragments", "lorebooks", "phrase_bank"]


# ── reverse policy validation (a stale/typo'd constant must be caught) ───────────


def test_coverage_flags_a_non_root_domain_key(tmp_path):
    """A DOMAIN_ROOTS key that is actually an owned child (not a true root) must be
    reported -- children inherit their root's domain, they cannot declare one."""
    conn = _fresh_schema_db(tmp_path)
    try:
        # messages is owned by conversations via ON DELETE CASCADE -> not a root.
        monkey = dict(presets.ps.DOMAIN_ROOTS, messages="chats")
        orig = presets.ps.DOMAIN_ROOTS
        presets.ps.DOMAIN_ROOTS = monkey
        try:
            problems = presets.schema_coverage_problems(conn)
        finally:
            presets.ps.DOMAIN_ROOTS = orig
        assert any("messages" in p and "not a true root" in p for p in problems), problems
    finally:
        conn.close()


def test_coverage_flags_a_stale_secret_column(tmp_path):
    """A SECRET_COLUMNS entry whose column no longer exists must be reported, not
    surface later as a raw OperationalError mid-export."""
    conn = _fresh_schema_db(tmp_path)
    try:
        monkey = dict(presets.ps.SECRET_COLUMNS)
        monkey[("settings", "ghost_token")] = ""
        orig = presets.ps.SECRET_COLUMNS
        presets.ps.SECRET_COLUMNS = monkey
        try:
            problems = presets.schema_coverage_problems(conn)
        finally:
            presets.ps.SECRET_COLUMNS = orig
        assert any("ghost_token" in p for p in problems), problems
    finally:
        conn.close()


# ── fresh-vs-migrated equivalence (the 0026 class of bug) ────────────────────────


def _strip_persona_lock_fk(conn: sqlite3.Connection, table: str) -> None:
    """Rebuild *table* with persona_lock_id as a bare INTEGER, mimicking a database
    migrated through 0026 but not yet 0027 (the silent-corruption shape)."""
    block = schema.table_create_sql(table).replace(" REFERENCES user_personas(id) ON DELETE SET NULL", "")
    block = block.replace(f"CREATE TABLE IF NOT EXISTS {table}", f"CREATE TABLE {table}_old", 1)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(block)
    cols = ",".join(r[1] for r in conn.execute(f"PRAGMA table_info({table}_old)"))
    conn.execute(f"INSERT INTO {table}_old ({cols}) SELECT {cols} FROM {table}")
    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {table}_old RENAME TO {table}")


def test_fresh_vs_migrated_equivalence_and_0027_repair(tmp_path):
    """The runtime gate must flag the pre-0027 persona_lock_id divergence (the exact
    0026 bug: an ALTER-added bare INTEGER where a fresh install has an FK), and
    migration 0027 must repair it so the live schema equals a fresh install again."""
    conn = _fresh_schema_db(tmp_path)
    try:
        for table in ("conversations", "character_cards"):
            _strip_persona_lock_fk(conn, table)
        conn.commit()

        # The gate names the divergence before 0027 runs.
        before = presets.schema_equivalence_problems(conn)
        assert any("conversations.persona_lock_id" in p for p in before), before
        assert any("character_cards.persona_lock_id" in p for p in before), before
        with pytest.raises(presets.PresetError):
            presets.assert_schema_safe(conn)

        # 0027 rebuilds both tables; the live schema then matches the canonical one.
        _mig_0027.migrate(conn)
        assert presets.schema_equivalence_problems(conn) == []
        for table in ("conversations", "character_cards"):
            assert _mig_0027._has_persona_lock_fk(conn, table)
        presets.assert_schema_safe(conn)  # no longer raises
    finally:
        conn.close()


def test_schema_safety_problems_is_non_fatal_but_preset_ops_stay_fatal(tmp_path):
    """The startup gate must not brick the app: ``schema_safety_problems`` reports the
    same divergence ``assert_schema_safe`` raises on, but returns it as a list instead
    of throwing -- so a schema quirk warns at boot while every preset op still fails
    hard on the identical problems."""
    conn = _fresh_schema_db(tmp_path)
    try:
        _strip_persona_lock_fk(conn, "conversations")
        conn.commit()

        # Non-fatal collector: returns the problems, never raises.
        problems = presets.schema_safety_problems(conn)
        assert any("conversations.persona_lock_id" in p for p in problems), problems

        # The hard gate used by export/apply/snapshot raises on the same list.
        with pytest.raises(presets.PresetError) as exc:
            presets.assert_schema_safe(conn)
        assert "conversations.persona_lock_id" in str(exc.value)

        # A clean schema yields no problems and the gate is silent.
        clean_dir = tmp_path / "clean"
        clean_dir.mkdir()
        clean = _fresh_schema_db(clean_dir)
        try:
            assert presets.schema_safety_problems(clean) == []
            presets.assert_schema_safe(clean)  # must not raise
        finally:
            clean.close()
    finally:
        conn.close()


def test_fully_migrated_fresh_install_satisfies_gate(tmp_path):
    """A real fresh install runs CREATE_TABLES_SQL *then every migration*, so the
    fully-migrated schema -- not raw CREATE_TABLES_SQL -- is what production boots
    with. It must satisfy the schema-safety gate. This is the integration guard that
    fails the day a migration leaves the live schema unlike CREATE_TABLES_SQL (a
    missing FK like 0026, a stale column like 0008's settings.active_model_config_id)
    -- a class the equivalence gate exists to stop reaching production."""
    from backend.database.migrations import run_pending

    db = tmp_path / "fresh.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(CREATE_TABLES_SQL)
    conn.commit()
    conn.close()
    run_pending(str(db))

    conn = sqlite3.connect(str(db))
    try:
        assert presets.schema_equivalence_problems(conn) == []
        assert presets.schema_coverage_problems(conn) == []
        presets.assert_schema_safe(conn)  # must not raise
    finally:
        conn.close()


# ── merge regressions (PR #90 audit) ────────────────────────────────────────────


def _seed(path: str, sql_pairs: list[tuple[str, tuple]]) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")  # may seed deliberately malformed source rows
        for sql, params in sql_pairs:
            conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _merge(main_path: str, preset_path: str, included: set, *, replace: bool = False) -> None:
    conn = sqlite3.connect(main_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("ATTACH DATABASE ? AS preset", (preset_path,))
        conn.execute("BEGIN")
        presets._merge(conn, included, replace)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.execute("COMMIT")
    finally:
        conn.execute("DETACH DATABASE preset")
        conn.close()


def test_persona_lock_survives_gapped_persona_ids(tmp_path):
    """Regression: a character locked to a file persona whose ids have gaps used to
    be double-remapped (phase C resolves it, phase E remapped it again through the
    same map), silently re-pointing it at the wrong persona. The lock must survive."""
    main, preset = str(tmp_path / "main.db"), str(tmp_path / "preset.db")
    for p in (main, preset):
        c = sqlite3.connect(p)
        c.executescript(CREATE_TABLES_SQL)
        c.commit()
        c.close()
    ts = "2024-01-01"
    # File personas have a gap: ids {2, 5} reinsert as {1, 2}, so new id 2 collides
    # with file old id 2 -- the trigger for the double remap.
    _seed(
        preset,
        [
            ("INSERT INTO user_personas (id, name, created_at, updated_at) VALUES (2, 'Alice', ?, ?)", (ts, ts)),
            ("INSERT INTO user_personas (id, name, created_at, updated_at) VALUES (5, 'Bob', ?, ?)", (ts, ts)),
            (
                "INSERT INTO character_cards (id, name, created_at, updated_at, persona_lock_id) "
                "VALUES ('char-1', 'Locked', ?, ?, 5)",
                (ts, ts),
            ),
        ],
    )
    _merge(main, preset, {"characters", "configs"})
    conn = sqlite3.connect(main)
    try:
        locked = conn.execute(
            "SELECT cc.name, up.name FROM character_cards cc LEFT JOIN user_personas up ON cc.persona_lock_id = up.id"
        ).fetchall()
    finally:
        conn.close()
    assert locked == [("Locked", "Bob")], locked


def test_orphan_surrogate_row_is_dropped_not_crashed(tmp_path):
    """Regression: a surrogate row dropped during insert (an external preset whose
    workflow_attachment points at an absent message) used to raise KeyError in the
    deferred fixup and abort the whole apply. It must be skipped instead."""
    main, preset = str(tmp_path / "main.db"), str(tmp_path / "preset.db")
    for p in (main, preset):
        c = sqlite3.connect(p)
        c.executescript(CREATE_TABLES_SQL)
        c.commit()
        c.close()
    ts = "2024-01-01"
    _seed(
        preset,
        [
            ("INSERT INTO conversations (id, title, created_at) VALUES ('c1', 't', ?)", (ts,)),
            (
                "INSERT INTO messages (id, conversation_id, role, content, turn_index, created_at) "
                "VALUES (10, 'c1', 'user', 'hi', 0, ?)",
                (ts,),
            ),
            (
                "INSERT INTO workflow_attachments (id, message_id, mime_type, data_b64, created_at, workflow_id) "
                "VALUES (7, 999, 'image/png', 'AAA', ?, 'wf')",
                (ts,),
            ),
        ],
    )
    _merge(main, preset, {"chats", "characters"})  # must not raise
    conn = sqlite3.connect(main)
    try:
        assert conn.execute("SELECT COUNT(*) FROM workflow_attachments").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        conn.close()


def test_self_parented_message_is_healed_to_root(tmp_path):
    """Regression: a self-parented (or cyclic) message in the source used to import
    as a faithful loop the app's tree-walk can spin on. The fixup must null the
    closing edge so the chain reaches root."""
    main, preset = str(tmp_path / "main.db"), str(tmp_path / "preset.db")
    for p in (main, preset):
        c = sqlite3.connect(p)
        c.executescript(CREATE_TABLES_SQL)
        c.commit()
        c.close()
    ts = "2024-01-01"
    _seed(
        preset,
        [
            ("INSERT INTO conversations (id, title, created_at) VALUES ('c1', 't', ?)", (ts,)),
            (
                "INSERT INTO messages (id, conversation_id, role, content, turn_index, parent_id, created_at) "
                "VALUES (10, 'c1', 'user', 'self', 0, 10, ?)",
                (ts,),
            ),
        ],
    )
    _merge(main, preset, {"chats", "characters"})
    conn = sqlite3.connect(main)
    try:
        parents = conn.execute("SELECT parent_id FROM messages").fetchall()
    finally:
        conn.close()
    assert parents == [(None,)], parents


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


# The tables the round-trip's _signature() actually reads (declared explicitly so a
# new table can't silently drop out of round-trip coverage -- see
# test_signature_covers_every_domain_table).
SIGNATURE_TABLES = frozenset(
    {
        "character_cards",
        "user_personas",
        "conversations",
        "messages",
        "director_state",
        "worlds",
        "lorebook_entries",
        "phrase_bank",
        "mood_fragments",
        "interactive_fragments",
    }
)

# Tables the round-trip deliberately does NOT signature-compare. Documented so the
# self-coverage test forces a conscious choice for every new table.
SIGNATURE_ALLOWLIST = frozenset(
    {
        # configs domain: round-trip asserts its presence via the summary, not content.
        "settings",
        "endpoints",
        "model_configs",
        # pure log / attachment tables: not part of any domain's user-facing identity.
        "conversation_logs",
        "user_attachments",
        "workflow_attachments",
    }
)


def _signature(path: str) -> dict:
    """Canonical, surrogate-id-independent content of every data domain.

    Surrogate ids (messages, personas, …) are never compared directly; references
    to them are resolved to the parent's portable identity (a persona's name, a
    leaf message's content) so two databases that differ only by autoincrement
    renumbering produce the same signature. The tables read here are pinned by
    ``SIGNATURE_TABLES`` and checked against the live schema below.
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
                "SELECT c.id, up.name FROM conversations c LEFT JOIN user_personas up ON c.persona_lock_id = up.id"
            ),
            "messages": q("SELECT conversation_id, turn_index, role, content FROM messages"),
            "active_leaf": q("SELECT c.id, m.content FROM conversations c LEFT JOIN messages m ON c.active_leaf_id = m.id"),
            "director_state": q("SELECT conversation_id FROM director_state"),
            "worlds": q("SELECT id, name FROM worlds"),
            "lorebook_entries": q("SELECT world_id, name, content FROM lorebook_entries"),
            "personas": q("SELECT name, description FROM user_personas"),
            "phrase_bank": q("SELECT variants, kind, pattern FROM phrase_bank"),
            "fragments": q("SELECT id, label FROM mood_fragments"),
            "interactive_fragments": q("SELECT id, label FROM interactive_fragments"),
        }
    finally:
        conn.close()


def test_signature_covers_every_domain_table(tmp_path):
    """Self-coverage: the tables _signature() reads, plus a documented allowlist of
    tables it deliberately skips, must partition the whole schema. Adding a table
    then fails this until the developer either extends _signature or consciously
    allowlists it -- a new table can never silently drop out of round-trip coverage."""
    conn = _fresh_schema_db(tmp_path)
    try:
        all_tables = set(presets._build_schema_model(conn).tables)
    finally:
        conn.close()
    assert SIGNATURE_TABLES & SIGNATURE_ALLOWLIST == set(), "a table is both signatured and allowlisted"
    assert SIGNATURE_TABLES | SIGNATURE_ALLOWLIST == all_tables, {
        "unaccounted (extend _signature or allowlist)": all_tables - SIGNATURE_TABLES - SIGNATURE_ALLOWLIST,
        "stale (not in schema)": (SIGNATURE_TABLES | SIGNATURE_ALLOWLIST) - all_tables,
    }


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

    # configs touch, plus a phrase-bank row (surrogate full-replace path) and a
    # mood fragment (stable upsert) so those domains carry real data round-trip.
    await client.put("/api/settings", json={"user_name": "Ada", "api_key": "sk-keep"})
    seed = sqlite3.connect(path)
    try:
        seed.execute("INSERT INTO phrase_bank (variants, kind, pattern) VALUES ('[\"hi\"]', 'literal', NULL)")
        seed.execute(
            "INSERT INTO mood_fragments (id, label, description, prompt_text) VALUES ('frag-1', 'Calm', 'desc', 'be calm')"
        )
        seed.commit()
    finally:
        seed.close()

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


# ── excluded-table tripwires (data must never hide in EXCLUDED_TABLES) ────────────


def test_excluded_data_tables_are_empty_in_fresh_schema(tmp_path):
    """Every excluded table other than the meta/migration bookkeeping must be empty
    on a fresh install -- excluded tables are invisible to export and merge, so any
    rows they carry would silently never be backed up."""
    conn = _fresh_schema_db(tmp_path)
    try:
        for tbl in presets.ps.EXCLUDED_TABLES:
            if tbl in presets._EXCLUDED_MAY_HAVE_ROWS:
                continue
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)).fetchone():
                continue
            assert conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0] == 0, tbl
    finally:
        conn.close()


async def test_build_preset_rejects_rows_in_excluded_table(client, db_path):
    """Runtime tripwire: parking real data in an excluded table must fail the export
    loudly rather than ship a backup that silently omits it."""
    import asyncio

    path = str(db_path)
    await client.post("/api/characters", json={"name": "Keep"})
    seed = sqlite3.connect(path)
    try:
        seed.execute("PRAGMA foreign_keys=OFF")  # message_attachments.message_id NOT NULL; we only need a row to exist
        seed.execute(
            "INSERT INTO message_attachments (message_id, mime_type, data_b64, created_at) "
            "VALUES (1, 'image/png', 'AAA', '2024-01-01')"
        )
        seed.commit()
    finally:
        seed.close()

    with pytest.raises(presets.PresetError) as exc:
        await asyncio.to_thread(presets.build_preset, ["characters"], False)
    assert "message_attachments" in str(exc.value)


# ── secret-canary leak sentinel ──────────────────────────────────────────────────


async def test_no_secret_canary_leaks_in_exports(client, db_path):
    """Seed a unique sentinel into every SECRET_COLUMNS column, then prove no leak
    path ships it: (a) any single domain exported without ``configs`` must contain
    no sentinel at all; (b) a full export with ``strip_keys`` must contain no
    *api_key* sentinel. A future leak fails this generically, not just for the
    declared columns' happy path."""
    path = str(db_path)

    def canary(table: str, col: str) -> bytes:
        return f"LEAK-CANARY-{table}-{col}".encode()

    seed = sqlite3.connect(path)
    try:
        for table, col in presets.ps.SECRET_COLUMNS:
            seed.execute(f"UPDATE {table} SET {col} = ?", (canary(table, col).decode(),))
        seed.commit()
    finally:
        seed.close()

    all_canaries = [canary(t, c) for (t, c) in presets.ps.SECRET_COLUMNS]
    api_key_canaries = [canary(t, c) for (t, c) in presets.ps.SECRET_COLUMNS if c == "api_key"]

    # (a) every single domain that does NOT pull in configs -> nothing personal ships.
    non_configs = [d for d in presets.ALL_DOMAINS if d != "configs"]
    for domain in non_configs:
        name = (await client.post("/api/presets/export", json={"domains": [domain], "strip_keys": False})).json()["name"]
        blob = open(presets._library_path(name), "rb").read()
        leaked = [c.decode() for c in all_canaries if c in blob]
        assert leaked == [], (domain, leaked)

    # (b) full export with strip_keys -> only the api_key sentinels must be gone.
    name = (await client.post("/api/presets/export", json={"domains": list(presets.ALL_DOMAINS), "strip_keys": True})).json()[
        "name"
    ]
    blob = open(presets._library_path(name), "rb").read()
    leaked_keys = [c.decode() for c in api_key_canaries if c in blob]
    assert leaked_keys == [], leaked_keys
