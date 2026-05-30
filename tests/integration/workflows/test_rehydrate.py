"""Tests for `POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate`.

Rehydrate writes bytes back into a sentinel-marked row in place using
stored seed + params. Preconditions: `data_b64 == EVICTED_MARKER` AND
`seed IS NOT NULL`. Verifies 404/409 surfaces, happy-path in-place
restore, counter bump.
"""

from __future__ import annotations

import base64

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.workflows.attachment_cache import EVICTED_MARKER, evict

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "rehydrate"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_with_seed(client, *, seed: str | None = "STORED-SEED") -> tuple[str, int, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"ORIGINAL",
            "workflow_id": "img",
            "seed": seed,
            "generation_metadata": {"steps": 4},
        },
    )
    return cid, mid, aid


async def test_unknown_conversation_returns_404(client):
    resp = await client.post(
        "/api/conversations/no-such/messages/1/workflow-attachments/1/rehydrate",
        json={},
    )
    assert resp.status_code == 404


async def test_attachment_not_found_returns_404(client):
    cid = await _new_conversation(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/1/workflow-attachments/99999/rehydrate",
        json={},
    )
    assert resp.status_code == 404


async def test_rehydrate_when_bytes_present_returns_409(client):
    cid, mid, aid = await _seed_with_seed(client)

    async def reroll(ctx, params, seed):
        return b"NEW"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    assert resp.status_code == 409
    assert "bytes are present" in resp.json()["detail"]


async def test_rehydrate_without_seed_returns_409(client):
    cid, mid, aid = await _seed_with_seed(client, seed=None)
    await evict(aid)

    async def reroll(ctx, params, seed):
        return b"NEW"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    assert resp.status_code == 409
    assert "seed" in resp.json()["detail"]


async def test_rehydrate_workflow_without_reroll_gen_returns_404(client):
    cid, mid, aid = await _seed_with_seed(client)
    await evict(aid)
    wf = make_workflow("img")  # no reroll_gen hook
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    assert resp.status_code == 404


async def test_rehydrate_happy_path_restores_bytes_in_place(client):
    cid, mid, aid = await _seed_with_seed(client)
    await evict(aid)

    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(seed)
        return b"RESTORED"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    assert resp.status_code == 200
    assert resp.json()["attachment_id"] == aid
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] == base64.b64encode(b"RESTORED").decode("ascii")
    assert captured == ["STORED-SEED"]


async def test_rehydrate_does_not_create_new_sibling(client):
    cid, mid, aid = await _seed_with_seed(client)
    await evict(aid)

    async def reroll(ctx, params, seed):
        return b"R"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    from backend.database import get_workflow_attachments_for_message

    rows = await get_workflow_attachments_for_message(mid)
    assert len(rows) == 1, "rehydrate is in-place; no new sibling"


async def test_rehydrate_resets_recent_accesses_to_single_fresh_counter(client, db):
    import json

    cid, mid, aid = await _seed_with_seed(client)
    # Seed stale pre-eviction history AND inflate the global counter so that
    # the post-rehydrate counter is unambiguously greater than the stale
    # entries -- proves the assertion below tests the reset, not coincidence.
    await db.execute(
        "UPDATE workflow_attachments SET recent_accesses = ? WHERE id = ?",
        (json.dumps([3, 2, 1]), aid),
    )
    await db.execute("UPDATE settings SET attachment_access_counter = 100 WHERE id = 1")
    await db.commit()
    await evict(aid)

    async def reroll(ctx, params, seed):
        return b"R"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    assert resp.status_code == 200
    rows = list(
        await db.execute_fetchall(
            "SELECT recent_accesses FROM workflow_attachments WHERE id = ?",
            (aid,),
        )
    )
    parsed = json.loads(rows[0]["recent_accesses"])
    assert len(parsed) == 1, "rehydrate must reset stale history, not prepend"
    assert parsed[0] > 3, "fresh counter must dominate stale entries to match birth-as-access"


async def test_rehydrate_counts_as_access(client, db):
    cid, mid, aid = await _seed_with_seed(client)
    await evict(aid)
    before = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]

    async def reroll(ctx, params, seed):
        return b"R"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    after = list(await db.execute_fetchall("SELECT attachment_access_counter FROM settings WHERE id = 1"))[0][
        "attachment_access_counter"
    ]
    assert after - before == 1


async def test_rehydrate_hook_raise_returns_500_and_keeps_sentinel(client):
    cid, mid, aid = await _seed_with_seed(client)
    await evict(aid)

    async def reroll(ctx, params, seed):
        raise RuntimeError("boom")

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    assert resp.status_code == 500
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] == EVICTED_MARKER


async def test_rehydrate_does_not_touch_active_sibling_id(client):
    cid, mid, aid = await _seed_with_seed(client)
    other = await insert_workflow_attachment_row(
        mid,
        {"filename": "y", "mime": "image/png", "data": b"Y", "workflow_id": "img", "parent_attachment_id": aid},
    )
    from backend.workflows.attachment_cache import set_active_sibling

    await set_active_sibling(aid, other)
    await evict(aid)

    async def reroll(ctx, params, seed):
        return b"R"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )
    root = await must_get_workflow_attachment(aid)
    assert root["active_sibling_id"] == other, "rehydrate is in-place; active pointer unchanged"
