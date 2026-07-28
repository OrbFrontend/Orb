"""Age-based data cleanup: /api/storage and /api/storage/cleanup.

The cleanup is deliberately asymmetric and these tests pin that asymmetry:

  - Artifacts are *evicted*, not deleted. The row and its recovery metadata
    survive so the image comes back through the normal rehydrate path, which is
    only true if the cleanup reuses the same sentinel the budget eviction uses.
  - Rows without recovery metadata (TTS audio stores no seed) are skipped, not
    destroyed -- for those the bytes are the only copy.
  - Agent logs have no recovery path and are a real DELETE.

The preview route must agree with the cleanup it previews, otherwise the age
choice in the UI is made against numbers that do not match the outcome.
"""

from __future__ import annotations

import pytest

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.database.queries.workflow_attachments import EVICTED_MARKER

from ._fixtures import make_workflow, register_for_test

OLD = "2020-01-01T00:00:00+00:00"
RECENT = "2999-01-01T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _register_wf_workflow():
    wf = make_workflow(
        "wf",
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(wf):
        yield


async def _conversation(client) -> tuple[str, int]:
    resp = await client.post("/api/conversations", json={"title": "Cleanup test"})
    assert resp.status_code == 200
    cid = resp.json()["id"]
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    return cid, mid


async def _attachment(db, mid: int, *, created_at: str, rehydratable: bool = True, data: bytes = b"payload-bytes") -> int:
    """Insert an artifact and backdate it. ``created_at`` is set by the insert,
    so the age is applied afterwards -- the same string format the column uses."""
    att: dict = {"filename": "x.png", "mime": "image/png", "data": data, "workflow_id": "wf"}
    if rehydratable:
        att["seed"] = "seed-1"
        att["generation_metadata"] = {"prompt": "a cat"}
    att_id = await insert_workflow_attachment_row(mid, att)
    await db.execute("UPDATE workflow_attachments SET created_at = ? WHERE id = ?", (created_at, att_id))
    await db.commit()
    return att_id


async def _data_b64(db, att_id: int) -> str:
    rows = list(await db.execute_fetchall("SELECT data_b64 FROM workflow_attachments WHERE id = ?", (att_id,)))
    return rows[0]["data_b64"]


async def test_cleanup_evicts_only_old_rehydratable_artifacts(client, db):
    _cid, mid = await _conversation(client)
    old_a = await _attachment(db, mid, created_at=OLD)
    old_b = await _attachment(db, mid, created_at=OLD)
    fresh = await _attachment(db, mid, created_at=RECENT)
    seedless = await _attachment(db, mid, created_at=OLD, rehydratable=False)

    resp = await client.post("/api/storage/cleanup", json={"artifacts": True, "days": 7})
    assert resp.status_code == 200
    assert resp.json()["artifacts_evicted"] == 2

    assert await _data_b64(db, old_a) == EVICTED_MARKER
    assert await _data_b64(db, old_b) == EVICTED_MARKER
    # Too new to be in scope, and no recovery metadata so eviction is refused.
    assert await _data_b64(db, fresh) != EVICTED_MARKER
    assert await _data_b64(db, seedless) != EVICTED_MARKER

    # Evict is not delete: every row, including the evicted ones, is still there.
    rows = list(await db.execute_fetchall("SELECT COUNT(*) AS n FROM workflow_attachments"))
    assert rows[0]["n"] == 4


async def test_cleanup_days_zero_means_everything(client, db):
    _cid, mid = await _conversation(client)
    fresh = await _attachment(db, mid, created_at=RECENT)

    resp = await client.post("/api/storage/cleanup", json={"artifacts": True, "days": 0})
    assert resp.status_code == 200
    assert resp.json()["artifacts_evicted"] == 1
    assert await _data_b64(db, fresh) == EVICTED_MARKER


async def test_unchecked_category_is_untouched(client, db):
    """Cleaning logs must not touch artifacts, and vice versa."""
    cid, mid = await _conversation(client)
    art = await _attachment(db, mid, created_at=OLD)
    await db.execute(
        "INSERT INTO conversation_logs (conversation_id, turn_index, injection_block, created_at) VALUES (?, 0, ?, ?)",
        (cid, "director said things", OLD),
    )
    await db.commit()

    resp = await client.post("/api/storage/cleanup", json={"logs": True, "days": 7})
    assert resp.status_code == 200
    assert resp.json()["logs_wiped"] == 1
    assert resp.json()["artifacts_evicted"] == 0
    assert await _data_b64(db, art) != EVICTED_MARKER


async def test_log_wipe_respects_cutoff_and_keeps_the_row(client, db):
    """The whole point of wiping over deleting: the row -- and the mood state the
    pipeline reads back off it -- survives, payload and all else does not."""
    cid, mid = await _conversation(client)
    # The fresh row is left unattached so the Inspector lookup below resolves to
    # the wiped one (it takes the newest log for the message).
    for created_at, message_id in ((OLD, mid), (RECENT, None)):
        await db.execute(
            "INSERT INTO conversation_logs (conversation_id, turn_index, created_at, message_id, "
            "active_moods_after, agent_latency_ms, tool_calls, reasoning_director, injection_block, feedback) "
            "VALUES (?, 0, ?, ?, ?, 1234, ?, ?, ?, ?)",
            (cid, created_at, message_id, '["calm"]', '[{"name": "set_mood"}]', "r" * 80, "i" * 100, '{"a": 1}'),
        )
    await db.commit()

    resp = await client.post("/api/storage/cleanup", json={"logs": True, "days": 7})
    assert resp.status_code == 200
    assert resp.json()["logs_wiped"] == 1

    rows = list(await db.execute_fetchall("SELECT * FROM conversation_logs ORDER BY created_at"))
    assert len(rows) == 2  # nothing deleted
    old, recent = rows
    assert (old["tool_calls"], old["reasoning_director"], old["injection_block"]) == (None, None, None)
    assert old["feedback"] == "{}"  # NOT NULL, so reset to its schema default
    assert old["active_moods_after"] == '["calm"]'  # whitelisted -- mood continuity
    assert old["agent_latency_ms"] == 1234  # whitelisted -- /api/stats averages it
    assert recent["injection_block"] == "i" * 100  # out of scope, untouched

    # A wiped turn must degrade to the empty log shape, not a 500.
    resp = await client.get(f"/api/conversations/{cid}/messages/{mid}/director-log")
    assert resp.status_code == 200
    assert resp.json()["tool_calls"] == []

    # Nothing reclaimable left: the preview agrees and a repeat run is a no-op.
    assert (await client.get("/api/storage?days=7")).json()["logs"]["count"] == 0
    assert (await client.get("/api/storage?days=0")).json()["logs"]["count"] == 1  # the fresh row, still in scope
    assert (await client.post("/api/storage/cleanup", json={"logs": True, "days": 7})).json()["logs_wiped"] == 0


async def test_wipe_covers_every_column_not_whitelisted(client, db):
    """Whitelist, not blacklist: a column added to the table later must be
    reclaimed by default. This fails the day one is added and forgotten."""
    from backend.database.queries.conversation_logs import LOG_KEEP_COLUMNS

    cols = {r["name"] for r in await db.execute_fetchall("PRAGMA table_info(conversation_logs)")}
    assert LOG_KEEP_COLUMNS <= cols, "whitelist names a column that no longer exists"
    # Everything else is payload. Named here only so the diff shows what a new
    # column joins -- the wipe itself needs no update.
    assert cols - LOG_KEEP_COLUMNS == {
        "tool_calls",
        "injection_block",
        "reasoning_director",
        "reasoning_writer",
        "reasoning_editor",
        "feedback",
        "fragment_diagnostics",
    }


async def test_preview_matches_what_cleanup_reports(client, db):
    _cid, mid = await _conversation(client)
    await _attachment(db, mid, created_at=OLD, data=b"a" * 900)
    await _attachment(db, mid, created_at=OLD, rehydratable=False, data=b"b" * 900)
    await _attachment(db, mid, created_at=RECENT, data=b"c" * 900)

    preview = (await client.get("/api/storage?days=7")).json()
    # Only the one old rehydratable row is in scope; the seedless and the fresh
    # row are both excluded from the preview exactly as they are from the work.
    assert preview["artifacts"]["count"] == 1
    assert preview["artifacts"]["bytes"] == 900

    resp = (await client.post("/api/storage/cleanup", json={"artifacts": True, "days": 7})).json()
    assert resp["artifacts_evicted"] == preview["artifacts"]["count"]

    # Preview is recomputed against the post-cleanup state: nothing left in scope.
    assert (await client.get("/api/storage?days=7")).json()["artifacts"]["count"] == 0


async def test_budget_setting_round_trips_and_has_a_floor(client):
    resp = await client.put("/api/settings", json={"attachment_cache_budget_bytes": 100 * 1024 * 1024})
    assert resp.status_code == 200
    assert resp.json()["attachment_cache_budget_bytes"] == 100 * 1024 * 1024

    # A fumbled 0 would blank the whole artifact cache on the next write.
    resp = await client.put("/api/settings", json={"attachment_cache_budget_bytes": 0})
    assert resp.status_code == 422


async def test_free_bytes_tracks_dead_pages_and_vacuum_returns_them(db, db_path):
    """The two halves of the startup gate: free_bytes must actually see dead
    pages (the db runs auto_vacuum=NONE, so a DELETE alone frees nothing on
    disk), and vacuum_sync must hand them back."""
    from backend.api.routes.storage import free_bytes, vacuum_sync

    await db.execute("CREATE TABLE bulk (id INTEGER PRIMARY KEY, blob TEXT)")
    await db.executemany("INSERT INTO bulk (blob) VALUES (?)", [("x" * 4000,) for _ in range(500)])
    await db.commit()
    assert free_bytes(str(db_path)) == 0

    await db.execute("DELETE FROM bulk")
    await db.commit()
    stranded = free_bytes(str(db_path))
    assert stranded > 0

    assert vacuum_sync(str(db_path)) is True
    assert free_bytes(str(db_path)) < stranded
