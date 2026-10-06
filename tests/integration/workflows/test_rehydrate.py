"""Tests for `POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate`.

Rehydrate writes bytes back into a sentinel-marked row in place using stored seed + params. Preconditions: `data_b64 ==
EVICTED_MARKER` AND `seed IS NOT NULL`. Verifies 404/409 surfaces, happy-path in-place restore, counter bump.
"""

import base64
import json

from backend.database import get_workflow_attachments_for_message, insert_workflow_attachment_row
from backend.workflows.attachment_cache import EVICTED_MARKER, evict, set_active_sibling

from ._fixtures import (
    attachment_action,
    make_workflow,
    must_get_workflow_attachment,
    register_for_test,
    reroll_workflow,
    returning,
    seed_attachment,
)


async def _evicted(client) -> tuple[str, int, int]:
    cid, mid, aid = await seed_attachment(client, data=b"ORIGINAL", seed="STORED-SEED")
    await evict(aid)
    return cid, mid, aid


async def _rehydrate(client, ids, value=b"R", calls=None, **body):
    with reroll_workflow(returning(value, calls)):
        return await attachment_action(client, *ids, "rehydrate", **body)


async def _access_counter(db) -> int:
    return (await db.one("SELECT attachment_access_counter FROM settings WHERE id = 1"))["attachment_access_counter"]


async def test_rehydrate_preconditions_return_409(client):
    response = await _rehydrate(client, await seed_attachment(client))
    assert response.status_code == 409 and "bytes are present" in response.json()["detail"]
    # Seed the invalid legacy row directly: the public eviction helper refuses to destroy rows that cannot be regenerated.
    response = await _rehydrate(client, await seed_attachment(client, seed=None, insert_as_evicted=True))
    assert response.status_code == 409 and "seed" in response.json()["detail"]


async def test_rehydrate_workflow_without_reroll_gen_returns_404(client):
    ids = await _evicted(client)
    with register_for_test(make_workflow("img")):  # no reroll_gen hook
        assert (await attachment_action(client, *ids, "rehydrate")).status_code == 404


async def test_rehydrate_happy_path_restores_bytes_in_place(client, db):
    ids = cid, mid, aid = await _evicted(client)
    before = await _access_counter(db)
    calls: list = []
    response = await _rehydrate(client, ids, b"RESTORED", calls, params={"steps": "999"})
    assert response.status_code == 200
    assert response.json()["attachment_id"] == aid
    assert (await must_get_workflow_attachment(aid))["data_b64"] == base64.b64encode(b"RESTORED").decode("ascii")
    # The stored seed and params, exactly: the override path is reroll-gen's alone.
    assert calls == [({"steps": 4}, "STORED-SEED")]
    assert len(await get_workflow_attachments_for_message(mid)) == 1, "rehydrate is in-place; no new sibling"
    assert await _access_counter(db) - before == 1


async def test_rehydrate_resets_recent_accesses_to_single_fresh_counter(client, db):
    cid, mid, aid = await seed_attachment(client)
    # Stale pre-eviction history and an inflated counter, so the fresh entry is unambiguously greater than the stale ones.
    await db.execute("UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?", (json.dumps([3, 2, 1]), aid))
    await db.execute("UPDATE settings SET attachment_access_counter = 100 WHERE id = 1")
    await db.commit()
    await evict(aid)
    assert (await _rehydrate(client, (cid, mid, aid))).status_code == 200
    row = await db.one("SELECT recent_accesses FROM workflow_attachments WHERE id = ?", (aid,))
    parsed = json.loads(row["recent_accesses"])
    assert len(parsed) == 1, "rehydrate must reset stale history, not prepend"
    assert parsed[0] > 3, "fresh counter must dominate stale entries to match birth-as-access"


async def test_rehydrate_hook_raise_returns_500_and_keeps_sentinel(client):
    ids = await _evicted(client)
    assert (await _rehydrate(client, ids, RuntimeError("boom"))).status_code == 500
    assert (await must_get_workflow_attachment(ids[2]))["data_b64"] == EVICTED_MARKER


async def test_rehydrate_does_not_touch_active_sibling_id(client):
    cid, mid, aid = await seed_attachment(client)
    other = await insert_workflow_attachment_row(
        mid, {"filename": "y", "mime": "image/png", "data": b"Y", "workflow_id": "img", "parent_attachment_id": aid}
    )
    await set_active_sibling(aid, other)
    await evict(aid)
    await _rehydrate(client, (cid, mid, aid))
    assert (await must_get_workflow_attachment(aid))["active_sibling_id"] == other, "rehydrate is in-place"
