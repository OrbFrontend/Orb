"""End-to-end tests for the preset / backup engine and its HTTP routes."""

from __future__ import annotations

import sqlite3


def _snap_dir(db_path):
    return db_path.parent / "snapshots"


async def _full_snapshot(client, label=""):
    """A restorable full-coverage snapshot, via the same route the UI uses."""
    from backend.presets import ALL_DOMAINS

    resp = await client.post(
        "/api/presets/export",
        json={"domains": list(ALL_DOMAINS), "strip_keys": False, "label": label},
    )
    return resp.json()["name"]


async def _make_conv_with_tree(db, cid="conv-1"):
    """Insert a conversation with a two-message branch + active leaf via raw SQL."""
    ts = "2024-01-01T00:00:00"
    await db.execute(
        "INSERT INTO conversations (id, title, created_at) VALUES (?, ?, ?)",
        (cid, "Tree Chat", ts),
    )
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


# ── export / library ─────────────────────────────────────────────────────


async def test_export_creates_library_entry(client, db_path):
    await client.post("/api/characters", json={"name": "Lira"})
    resp = await client.post("/api/presets/export", json={"domains": ["characters"], "label": "cast"})
    assert resp.status_code == 200
    name = resp.json()["name"]
    assert (_snap_dir(db_path) / name).exists()

    lst = await client.get("/api/presets")
    entries = lst.json()
    assert len(entries) == 1
    assert entries[0]["kind"] == "manual"
    assert entries[0]["included_domains"] == ["characters"]


async def test_export_empty_domains_rejected(client):
    resp = await client.post("/api/presets/export", json={"domains": []})
    assert resp.status_code == 400


async def test_chats_export_forces_characters(client, db_path):
    resp = await client.post("/api/presets/export", json={"domains": ["chats"]})
    name = resp.json()["name"]
    meta = sqlite3.connect(str(_snap_dir(db_path) / name)).execute("SELECT included_domains FROM orb_preset_meta").fetchone()[0]
    assert "characters" in meta and "chats" in meta


# ── apply (merge) ──────────────────────────────────────────────────────────


async def test_apply_readds_deleted_and_preserves_new(client, db):
    keep = (await client.post("/api/characters", json={"name": "Keep"})).json()["id"]
    temp = (await client.post("/api/characters", json={"name": "Temp"})).json()["id"]
    name = (await client.post("/api/presets/export", json={"domains": ["characters"]})).json()["name"]

    await client.delete(f"/api/characters/{keep}")
    await client.delete(f"/api/characters/{temp}")
    await client.post("/api/characters", json={"name": "Fresh"})

    resp = await client.post(f"/api/presets/{name}/apply", json={})
    assert resp.status_code == 200

    names = {c["name"] for c in (await client.get("/api/characters")).json()}
    assert {"Keep", "Temp", "Fresh"} <= names


async def test_apply_overwrites_by_id(client):
    cid = (await client.post("/api/characters", json={"name": "Orig"})).json()["id"]
    name = (await client.post("/api/presets/export", json={"domains": ["characters"]})).json()["name"]

    await client.put(f"/api/characters/{cid}", json={"name": "Changed"})
    await client.post(f"/api/presets/{name}/apply", json={})

    assert (await client.get(f"/api/characters/{cid}")).json()["name"] == "Orig"


async def test_apply_restores_chat_tree(client, db):
    m1, m2 = await _make_conv_with_tree(db)
    name = (await client.post("/api/presets/export", json={"domains": ["chats"]})).json()["name"]

    await client.delete("/api/conversations/conv-1")
    assert (await client.get("/api/conversations/conv-1/messages")).status_code in (200, 404)

    resp = await client.post(f"/api/presets/{name}/apply", json={})
    assert resp.status_code == 200

    # Conversation + its two messages are back; the branch link survives a remap.
    async with db.execute("SELECT active_leaf_id FROM conversations WHERE id = 'conv-1'") as cur:
        leaf = (await cur.fetchone())["active_leaf_id"]
    async with db.execute(
        "SELECT id, parent_id, role, content FROM messages WHERE conversation_id = 'conv-1' ORDER BY turn_index"
    ) as cur:
        rows = await cur.fetchall()
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
    name = (await client.post("/api/presets/export", json={"domains": ["chats"]})).json()["name"]

    resp = await client.post(f"/api/presets/{name}/apply", json={})
    assert resp.status_code == 200

    async with db.execute("SELECT COUNT(*) AS n FROM messages WHERE conversation_id = 'conv-1'") as cur:
        assert (await cur.fetchone())["n"] == 2  # not 4
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


async def test_apply_configs_leaves_no_orphaned_model_configs(client, db):
    """A full-domain preset including configs must merge without the FK check
    aborting on model_configs whose endpoint was deleted but not cascaded."""
    eid = (await client.post("/api/endpoints", json={"url": "http://x"})).json()["id"]
    await client.post(f"/api/endpoints/{eid}/models", json={"model_name": "m1"})
    name = (await client.post("/api/presets/export", json={"domains": ["configs"]})).json()["name"]

    resp = await client.post(f"/api/presets/{name}/apply", json={})
    assert resp.status_code == 200, resp.json()

    async with db.execute("SELECT COUNT(*) AS n FROM model_configs WHERE endpoint_id NOT IN (SELECT id FROM endpoints)") as cur:
        assert (await cur.fetchone())["n"] == 0
    async with db.execute("PRAGMA foreign_key_check") as cur:
        assert await cur.fetchall() == []


# ── configs / key stripping ────────────────────────────────────────────────


async def test_export_strips_api_keys_by_default(client, db_path):
    await client.put("/api/settings", json={"api_key": "sk-secret"})
    name = (await client.post("/api/presets/export", json={"domains": ["configs"], "strip_keys": True})).json()["name"]
    conn = sqlite3.connect(str(_snap_dir(db_path) / name))
    assert conn.execute("SELECT api_key FROM settings WHERE id=1").fetchone()[0] == ""
    assert all(r[0] == "" for r in conn.execute("SELECT api_key FROM endpoints").fetchall())


async def test_export_keeps_keys_when_not_stripped(client, db_path):
    await client.put("/api/settings", json={"api_key": "sk-secret"})
    name = (await client.post("/api/presets/export", json={"domains": ["configs"], "strip_keys": False})).json()["name"]
    conn = sqlite3.connect(str(_snap_dir(db_path) / name))
    assert conn.execute("SELECT api_key FROM settings WHERE id=1").fetchone()[0] == "sk-secret"


async def test_export_without_configs_scrubs_keys(client, db_path):
    await client.put("/api/settings", json={"api_key": "sk-secret"})
    name = (await client.post("/api/presets/export", json={"domains": ["characters"]})).json()["name"]
    conn = sqlite3.connect(str(_snap_dir(db_path) / name))
    assert conn.execute("SELECT api_key FROM settings WHERE id=1").fetchone()[0] == ""


# ── snapshot / restore ─────────────────────────────────────────────────────


async def test_restore_is_full_rollback(client, db):
    await client.post("/api/characters", json={"name": "Before"})
    snap = await _full_snapshot(client, "safe")

    await client.post("/api/characters", json={"name": "After"})
    await client.post(f"/api/presets/{snap}/restore", json={})

    names = {c["name"] for c in (await client.get("/api/characters")).json()}
    assert "Before" in names
    assert "After" not in names  # full replace drops post-snapshot additions


async def test_restore_succeeds_with_open_connection(client, db_path):
    """Regression: restoring while another connection holds the live DB open
    (as the running app does for any overlapping request) used to fail with
    'database is locked' because the file/WAL was swapped out from under it."""
    from backend import presets

    await client.post("/api/characters", json={"name": "Before"})
    snap = await _full_snapshot(client, "safe")
    await client.post("/api/characters", json={"name": "After"})

    holder = sqlite3.connect(str(db_path))
    try:
        holder.execute("PRAGMA journal_mode=WAL")
        holder.execute("SELECT 1 FROM character_cards").fetchall()  # hold a read lock
        presets.restore_full(snap)  # must not raise OperationalError
    finally:
        holder.close()

    names = {c["name"] for c in (await client.get("/api/characters")).json()}
    assert "Before" in names
    assert "After" not in names


async def test_apply_takes_auto_backup(client):
    await client.post("/api/characters", json={"name": "X"})
    name = (await client.post("/api/presets/export", json={"domains": ["characters"]})).json()["name"]
    resp = await client.post(f"/api/presets/{name}/apply", json={})
    backup = resp.json()["backup"]
    lst = {e["name"]: e for e in (await client.get("/api/presets")).json()}
    assert lst[backup]["kind"] == "auto"


# ── import upload + version skew ───────────────────────────────────────────


async def test_import_upload_merges(client, db_path):
    cid = (await client.post("/api/characters", json={"name": "Imported"})).json()["id"]
    name = (await client.post("/api/presets/export", json={"domains": ["characters"]})).json()["name"]
    blob = (_snap_dir(db_path) / name).read_bytes()

    await client.delete(f"/api/characters/{cid}")
    resp = await client.post(
        "/api/presets/import",
        files={"file": ("shared.db", blob, "application/octet-stream")},
    )
    assert resp.status_code == 200
    names = {c["name"] for c in (await client.get("/api/characters")).json()}
    assert "Imported" in names

    # The uploaded file was a "manual" export, but in this library it is now an
    # imported preset -- the "imported" kind overrides the embedded one, while
    # its partial domain coverage is preserved.
    imported = [e for e in (await client.get("/api/presets")).json() if e["kind"] == "imported"]
    assert len(imported) == 1
    assert imported[0]["included_domains"] == ["characters"]


async def test_import_rejects_newer_schema(client, db_path):
    name = (await client.post("/api/presets/export", json={"domains": ["characters"]})).json()["name"]
    path = _snap_dir(db_path) / name
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO schema_migrations (id) VALUES ('9999_from_the_future')")
    conn.commit()
    conn.close()
    blob = path.read_bytes()

    resp = await client.post(
        "/api/presets/import",
        files={"file": ("future.db", blob, "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "newer version" in resp.json()["detail"]


async def test_import_rejects_non_db(client):
    resp = await client.post(
        "/api/presets/import",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert resp.status_code == 400


def test_library_path_rejects_traversal():
    """A request-supplied name must stay inside the snapshots dir."""
    import pytest

    from backend import presets

    for bad in ("../secret.db", "sub/dir.db", "/etc/passwd", "..\\evil.db", "noext"):
        with pytest.raises(presets.PresetError):
            presets._library_path(bad)
