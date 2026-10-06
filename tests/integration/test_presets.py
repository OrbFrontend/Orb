"""End-to-end tests for the preset / backup engine and its HTTP routes."""

import sqlite3
from contextlib import closing


def _snap_dir(db_path):
    return db_path.parent / "snapshots"


async def _full_snapshot(client, label=""):
    """A restorable full-coverage snapshot, via the same route the UI uses."""
    from backend.features.presets import ALL_DOMAINS

    resp = await client.post("/api/presets/export", json={"domains": list(ALL_DOMAINS), "strip_keys": False, "label": label})
    return resp.json()["name"]


async def _make_conv_with_tree(db, cid="conv-1"):
    """Insert a conversation with a two-message branch + active leaf via raw SQL."""
    ts = "2024-01-01T00:00:00"
    await db.execute("INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)", (cid, "Tree Chat", ts))
    cur = await db.execute(
        "INSERT INTO messages (conversation_id, role, content, turn_index, parent_id, created_at) "
        "VALUES (?, 'user', 'hello', 0, NULL, ?)",
        (cid, ts),
    )
    m1 = cur.lastrowid
    cur = await db.execute(
        "INSERT INTO messages (conversation_id, role, content, turn_index, parent_id, created_at) "
        "VALUES (?, 'assistant', 'hi there', 1, ?, ?)",
        (cid, m1, ts),
    )
    m2 = cur.lastrowid
    await db.execute("UPDATE conversations SET active_leaf_id = ? WHERE id = ?", (m2, cid))
    await db.execute("INSERT INTO director_state (conversation_id, active_moods) VALUES (?, '[]')", (cid,))
    await db.commit()
    return m1, m2


# -- export / library -----------------------------------------------------


async def test_export_creates_library_entry(client, db_path):
    await client.post("/api/characters", json={"name": "Lira"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"], "label": "cast"}))["name"]
    assert (_snap_dir(db_path) / name).exists()

    entries = (await client.get("/api/presets")).json()
    assert len(entries) == 1
    assert entries[0]["kind"] == "manual"
    assert entries[0]["included_domains"] == ["characters"]


async def test_export_empty_domains_rejected(client):
    await client.post_checked("/api/presets/export", json={"domains": []}, expected_status=400)


async def test_chats_export_forces_characters(client, db_path):
    name = (await client.post("/api/presets/export", json={"domains": ["chats"]})).json()["name"]
    meta = sqlite3.connect(str(_snap_dir(db_path) / name)).execute("SELECT included_domains FROM orb_preset_meta").fetchone()[0]
    assert "characters" in meta and "chats" in meta


# -- apply (merge) ----------------------------------------------------------


async def test_apply_readds_deleted_and_preserves_new(client, db):
    keep = await client.create("/api/characters", json={"name": "Keep"})
    temp = await client.create("/api/characters", json={"name": "Temp"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]

    await client.delete(f"/api/characters/{keep}")
    await client.delete(f"/api/characters/{temp}")
    await client.post("/api/characters", json={"name": "Fresh"})

    await client.post_checked(f"/api/presets/{name}/apply", json={})

    assert {"Keep", "Temp", "Fresh"} <= {c["name"] for c in await client.get_json("/api/characters")}


async def test_apply_overwrites_by_id(client):
    cid = await client.create("/api/characters", json={"name": "Orig"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]

    await client.put(f"/api/characters/{cid}", json={"name": "Changed"})
    await client.post(f"/api/presets/{name}/apply", json={})

    assert (await client.get_json(f"/api/characters/{cid}"))["name"] == "Orig"


async def test_apply_restores_chat_tree(client, db):
    m1, m2 = await _make_conv_with_tree(db)
    name = (await client.post_json("/api/presets/export", json={"domains": ["chats"]}))["name"]

    await client.delete("/api/conversations/conv-1")
    assert (await client.get("/api/conversations/conv-1/messages")).status_code in (200, 404)

    await client.post_checked(f"/api/presets/{name}/apply", json={})

    # Conversation + its two messages are back; the branch link survives a remap.
    async with db.execute("SELECT active_leaf_id FROM conversations WHERE id = 'conv-1'") as cur:
        leaf = (await cur.fetchone())["active_leaf_id"]
    rows = await db.all(
        "SELECT id, parent_id, role, content FROM messages WHERE conversation_id = 'conv-1' ORDER BY turn_index"
    )
    assert [r["content"] for r in rows] == ["hello", "hi there"]
    assert rows[1]["parent_id"] == rows[0]["id"]  # child still points at parent
    assert leaf == rows[1]["id"]  # active leaf remapped to the new id
    # No dangling foreign keys.
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


async def test_apply_chats_does_not_duplicate_tree(client, db):
    """Applying a chats preset over the same live data replaces the subtree
    rather than stacking a second copy beside it (apply runs FK-off, so the
    cascade that was meant to clear the old messages never fires)."""
    await _make_conv_with_tree(db)
    name = (await client.post_json("/api/presets/export", json={"domains": ["chats"]}))["name"]

    await client.post_checked(f"/api/presets/{name}/apply", json={})

    async with db.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = 'conv-1'") as cur:
        assert (await cur.fetchone())["n"] == 2  # not 4
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


async def test_apply_configs_leaves_no_orphaned_model_configs(client, db):
    """A full-domain preset including configs must merge without the FK check
    aborting on model_configs whose endpoint was deleted but not cascaded."""
    eid = await client.create("/api/endpoints", json={"url": "http://x"})
    await client.post(f"/api/endpoints/{eid}/models", json={"model_name": "m1"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["configs"]}))["name"]

    await client.post_checked(f"/api/presets/{name}/apply", json={})

    async with db.execute("SELECT COUNT(*) AS n FROM model_configs WHERE endpoint_id NOT IN (SELECT id FROM endpoints)") as cur:
        assert (await cur.fetchone())["n"] == 0
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


# -- configs / key stripping ------------------------------------------------


async def _set_active_endpoint_key(client, api_key: str) -> None:
    endpoint_id = (await client.get_json("/api/settings"))["active_endpoint_id"]
    await client.put_checked(f"/api/endpoints/{endpoint_id}", json={"api_key": api_key})


async def test_export_strips_api_keys_by_default(client, db_path):
    await _set_active_endpoint_key(client, "sk-secret")
    name = (await client.post_json("/api/presets/export", json={"domains": ["configs"], "strip_keys": True}))["name"]
    conn = sqlite3.connect(str(_snap_dir(db_path) / name))
    keys = [r[0] for r in conn.execute("SELECT api_key FROM endpoints").fetchall()]
    assert keys and all(key == "" for key in keys)


async def test_export_without_configs_scrubs_keys(client, db_path):
    await _set_active_endpoint_key(client, "sk-secret")
    await client.put_checked("/api/settings", json={"system_prompt": "private"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]
    conn = sqlite3.connect(str(_snap_dir(db_path) / name))
    assert conn.execute("SELECT COUNT(*) FROM endpoints").fetchone()[0] == 0
    assert conn.execute("SELECT system_prompt FROM settings WHERE id=1").fetchone()[0] == ""


# -- snapshot / restore -----------------------------------------------------


async def test_restore_is_full_rollback(client, db):
    await client.post("/api/characters", json={"name": "Before"})
    snap = await _full_snapshot(client, "safe")

    await client.post("/api/characters", json={"name": "After"})
    await client.post(f"/api/presets/{snap}/restore", json={})

    names = {c["name"] for c in await client.get_json("/api/characters")}
    assert "Before" in names
    assert "After" not in names  # full replace drops post-snapshot additions


async def test_restore_succeeds_with_open_connection(client, db_path):
    """Regression: restoring while another connection holds the live DB open (as the running app does for any overlapping
    request) used to fail with 'database is locked' because the file/WAL was swapped out from under it.

    The same case is what makes the restore portable. Replacing the live file by renaming a prepared one over it is POSIX-only:
    Windows opens files without FILE_SHARE_DELETE, so that rename fails with PermissionError [WinError 5] whenever anyone --
    this holder, or any overlapping request -- still has the database open. Keep the copy going through SQLite (restore_full's
    online backup), not through the filesystem.
    """
    from backend.features.presets import engine as presets

    await client.post("/api/characters", json={"name": "Before"})
    snap = await _full_snapshot(client, "safe")
    await client.post("/api/characters", json={"name": "After"})

    with closing(sqlite3.connect(str(db_path))) as holder:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("SELECT 1 FROM character_cards").fetchall()  # hold a read lock
        presets.restore_full(snap)  # must not raise OperationalError

    names = {c["name"] for c in await client.get_json("/api/characters")}
    assert "Before" in names
    assert "After" not in names


async def test_restore_realigns_mismatched_page_size(client, db_path):
    """A library file whose page size differs from the live DB still restores.

    The live DB runs in WAL mode, and SQLite cannot change a WAL database's page size, so copying such a file in fails with a
    bare "attempt to write a readonly database" unless the prepared copy is rebuilt to match first. Only reachable via a preset
    imported from an install configured differently -- a locally produced one inherits the live page size.
    """
    from backend.features.presets import engine as presets

    await client.post("/api/characters", json={"name": "Before"})
    snap = await _full_snapshot(client, "repaged")
    await client.post("/api/characters", json={"name": "After"})

    with closing(sqlite3.connect(str(db_path))) as live:
        live_page = live.execute("PRAGMA page_size").fetchone()[0]
        assert live.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    with closing(sqlite3.connect(str(_snap_dir(db_path) / snap), isolation_level=None)) as conn:
        conn.execute(f"PRAGMA page_size={live_page * 2}")
        conn.execute("VACUUM")  # a page-size change only lands on rebuild
        assert conn.execute("PRAGMA page_size").fetchone()[0] == live_page * 2

    presets.restore_full(snap)

    assert {c["name"] for c in await client.get_json("/api/characters")} == {"Before"}


async def test_apply_takes_auto_backup(client):
    await client.post("/api/characters", json={"name": "X"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]
    backup = (await client.post(f"/api/presets/{name}/apply", json={})).json()["backup"]
    assert {e["name"]: e for e in await client.get_json("/api/presets")}[backup]["kind"] == "auto"


# -- partial restore (domain-scoped replace) --------------------------------


async def test_partial_restore_replaces_covered_domain(client):
    """Restoring a characters-only backup makes characters match the file
    exactly: post-backup additions are dropped and edits reverted -- unlike
    apply, which keeps them (see test_apply_readds_deleted_and_preserves_new)."""
    keep = await client.create("/api/characters", json={"name": "Keep"})
    await client.post("/api/characters", json={"name": "Temp"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]

    await client.put(f"/api/characters/{keep}", json={"name": "Edited"})
    await client.post("/api/characters", json={"name": "Fresh"})

    await client.post_checked(f"/api/presets/{name}/restore", json={})

    assert {c["name"] for c in await client.get_json("/api/characters")} == {"Keep", "Temp"}  # Fresh dropped, Keep reverted


async def test_partial_restore_leaves_other_domains_untouched(client, db):
    """A characters-only restore must not disturb an uncovered domain (chats)."""
    await _make_conv_with_tree(db)
    await client.post("/api/characters", json={"name": "Solo"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]

    await client.post_checked(f"/api/presets/{name}/restore", json={})

    async with db.execute("SELECT COUNT(*) AS n FROM conversations WHERE id = 'conv-1'") as cur:
        assert (await cur.fetchone())["n"] == 1
    async with db.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = 'conv-1'") as cur:
        assert (await cur.fetchone())["n"] == 2


async def test_partial_restore_nulls_dangling_world(client, db):
    """Restoring a lorebooks backup that no longer carries a world a character
    points at nulls the dangling link rather than corrupting foreign keys."""
    await client.post("/api/worlds", json={"name": "W1"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["lorebooks"]}))["name"]

    # A world (and character link) created *after* the backup: the restore must
    # drop W2 (worlds end up matching the file = {W1}) and null the stale link.
    w2 = await client.create("/api/worlds", json={"name": "W2"})
    ch = await client.create("/api/characters", json={"name": "Linked"})
    await client.put(f"/api/characters/{ch}", json={"world_id": w2})

    await client.post_checked(f"/api/presets/{name}/restore", json={})

    async with db.execute("SELECT world_id FROM character_cards WHERE id = ?", (ch,)) as cur:
        assert (await cur.fetchone())["world_id"] is None
    async with db.execute("SELECT COUNT(*) AS n FROM worlds") as cur:
        assert (await cur.fetchone())["n"] == 1  # only W1 survives
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


async def test_apply_nulls_dangling_character_persona_lock(client, db):
    """A characters backup carries character_cards.persona_lock_id but not the
    user_personas it points at (those live in the configs domain). Applying it
    where the locked persona no longer exists must leave no dangling FK that
    aborts the whole import. (On a fresh-schema DB the export-time scrub already
    nulls the lock; this guards the end state for the migrated, FK-less case.)"""
    pid = await client.create("/api/user-personas", json={"name": "Pinned"})
    ch = await client.create("/api/characters", json={"name": "Locked"})
    await client.put(f"/api/characters/{ch}", json={"persona_lock_id": pid})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]

    # Persona gone after the backup: delete_user_persona clears the *live* lock,
    # but the exported file still carries persona_lock_id = pid.
    await client.delete(f"/api/user-personas/{pid}")

    await client.post_checked(f"/api/presets/{name}/apply", json={})

    async with db.execute("SELECT persona_lock_id FROM character_cards WHERE id = ?", (ch,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


async def test_apply_remaps_persona_lock_when_configs_included(client, db):
    """When configs travels with characters, personas are re-keyed on import
    (fresh auto-increment ids). A character's persona_lock_id must follow that
    remap so the pin survives instead of binding to the wrong persona / dangling."""
    pid = await client.create("/api/user-personas", json={"name": "Pinned"})
    ch = await client.create("/api/characters", json={"name": "Locked"})
    await client.put(f"/api/characters/{ch}", json={"persona_lock_id": pid})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters", "configs"]}))["name"]

    await client.post_checked(f"/api/presets/{name}/apply", json={})

    # The lock still resolves to the persona named "Pinned", whatever its new id.
    async with db.execute("SELECT persona_lock_id FROM character_cards WHERE id = ?", (ch,)) as cur:
        locked_id = (await cur.fetchone())["persona_lock_id"]
    assert locked_id is not None
    async with db.execute("SELECT name FROM user_personas WHERE id = ?", (locked_id,)) as cur:
        assert (await cur.fetchone())["name"] == "Pinned"
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


async def test_apply_configs_clears_orphaned_local_persona_lock(client, db):
    """Importing configs re-keys every user_persona, so a pre-existing local
    character lock pointing at a now-replaced persona must be cleared rather
    than left dangling and aborting the import."""
    name = (await client.post_json("/api/presets/export", json={"domains": ["configs"]}))["name"]

    # Local persona + locked character created *after* the configs backup; the
    # configs merge wipes/re-keys user_personas, orphaning this lock.
    pid = await client.create("/api/user-personas", json={"name": "Local"})
    ch = await client.create("/api/characters", json={"name": "Locked"})
    await client.put(f"/api/characters/{ch}", json={"persona_lock_id": pid})

    await client.post_checked(f"/api/presets/{name}/apply", json={})

    async with db.execute("SELECT persona_lock_id FROM character_cards WHERE id = ?", (ch,)) as cur:
        assert (await cur.fetchone())["persona_lock_id"] is None
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


async def test_restore_overwrites_imported(client, db_path):
    """Imported backups can be restored (overwrite), not just applied: the
    covered domain ends up matching the file, with the auto-backup as the
    safety net that makes that reversible."""
    await client.post("/api/characters", json={"name": "Keep"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]
    blob = (_snap_dir(db_path) / name).read_bytes()
    stored = (await client.post_json("/api/presets/import", files={"file": ("ext.db", blob, "application/octet-stream")}))[
        "name"
    ]

    await client.post("/api/characters", json={"name": "Fresh"})  # added after the backup
    await client.post_checked(f"/api/presets/{stored}/restore", json={})

    names = {c["name"] for c in await client.get_json("/api/characters")}
    assert names == {"Keep"}  # Fresh dropped -- characters match the imported file


# -- import upload + version skew -------------------------------------------


async def test_import_lands_in_library_non_destructively(client, db_path):
    cid = await client.create("/api/characters", json={"name": "Imported"})
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]
    blob = (_snap_dir(db_path) / name).read_bytes()

    await client.delete(f"/api/characters/{cid}")
    await client.post_checked("/api/presets/import", files={"file": ("shared.db", blob, "application/octet-stream")})
    # Import only stocks the library -- it does not touch live data, so the
    # deleted character is NOT brought back (the user applies/restores to do that).
    assert "Imported" not in {c["name"] for c in await client.get_json("/api/characters")}

    # The uploaded file was a "manual" export, but in this library it is now an imported preset -- the "imported" kind overrides
    # the embedded one, while its partial domain coverage is preserved.
    imported = [e for e in await client.get_json("/api/presets") if e["kind"] == "imported"]
    assert len(imported) == 1
    assert imported[0]["included_domains"] == ["characters"]


async def test_import_rejects_newer_schema(client, db_path):
    name = (await client.post_json("/api/presets/export", json={"domains": ["characters"]}))["name"]
    path = _snap_dir(db_path) / name
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO schema_migrations (id) VALUES ('9999_from_the_future')")
    conn.commit()
    conn.close()
    blob = path.read_bytes()

    resp = await client.post_json(
        "/api/presets/import", files={"file": ("future.db", blob, "application/octet-stream")}, expected_status=400
    )
    assert "newer version" in resp["detail"]


async def test_import_rejects_non_db(client):
    await client.post_checked("/api/presets/import", files={"file": ("notes.txt", b"hello", "text/plain")}, expected_status=400)


def test_library_path_rejects_traversal():
    """A request-supplied name must stay inside the snapshots dir."""
    import pytest

    from backend.features.presets import engine as presets

    for bad in ("../secret.db", "sub/dir.db", "/etc/passwd", "..\\evil.db", "noext"):
        with pytest.raises(presets.PresetError):
            presets._library_path(bad)


async def test_apply_preserves_local_workflow_toggles(client):
    """Which workflows an install has disabled -- and which opt-in local-ML features it has running -- is local operational
    state, not content a shared preset should dictate. All three toggle columns are in PRESERVED_COLUMNS, so applying a
    configs preset must not re-enable something the user turned off locally.
    """
    from backend.database import get_settings, set_local_ml_enabled, set_workflow_enabled

    # Snapshot configs while the toggles sit at their on-state defaults.
    name = (await client.post_json("/api/presets/export", json={"domains": ["configs"]}))["name"]

    # Flip them locally off, then apply the on-state preset over them.
    await client.put("/api/settings", json={"workflows_globally_enabled": False})
    await set_workflow_enabled("tts", False)
    await set_local_ml_enabled("autocomplete", False)
    await client.post_checked(f"/api/presets/{name}/apply", json={})

    s = await get_settings()
    assert s["workflows_globally_enabled"] == 0
    assert s.get("workflow_enabled") == {"tts": False}
    assert s.get("local_ml_enabled") == {"autocomplete": False}
