"""Tests for `POST .../workflow-attachments/{aid}/reroll-gen`.

Pins the reroll-gen contract: the new sibling inherits the original's
generation_metadata verbatim while the hook is invoked with a freshly
minted seed, so an evict-then-rehydrate cycle on the sibling reproduces
its output deterministically.
"""

from __future__ import annotations

import json

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "reroll-gen"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_with_metadata(client) -> tuple[str, int, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"OG",
            "workflow_id": "img",
            "seed": "ORIG-SEED",
            "generation_metadata": {"steps": 4},
        },
    )
    return cid, mid, aid


async def test_unknown_conversation_returns_404(client):
    resp = await client.post(
        "/api/conversations/no-such/messages/1/workflow-attachments/1/reroll-gen",
        json={},
    )
    assert resp.status_code == 404


async def test_attachment_not_found_returns_404(client):
    cid = await _new_conversation(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/1/workflow-attachments/99999/reroll-gen",
        json={},
    )
    assert resp.status_code == 404


async def test_workflow_without_reroll_gen_hook_returns_404(client):
    cid, mid, aid = await _seed_with_metadata(client)
    wf = make_workflow("img")  # no reroll_gen
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 404


async def test_happy_path_inserts_new_sibling_with_fresh_seed_and_same_params(client):
    cid, mid, aid = await _seed_with_metadata(client)
    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append((dict(params), seed))
        return b"NEW_BYTES"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 200
    new_id = resp.json()["attachment_id"]
    new_row = await must_get_workflow_attachment(new_id)
    assert new_row["parent_attachment_id"] == aid
    assert new_row["workflow_id"] == "img"
    assert json.loads(new_row["generation_metadata"]) == {"steps": 4}
    assert new_row["seed"] != "ORIG-SEED"
    assert isinstance(new_row["seed"], str) and len(new_row["seed"]) == 32
    params_passed, seed_passed = captured[0]
    assert params_passed == {"steps": 4}
    assert seed_passed == new_row["seed"]


async def test_dispatcher_marks_active_sibling_to_new_id(client):
    cid, mid, aid = await _seed_with_metadata(client)

    async def reroll(ctx, params, seed):
        return b"B"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    new_id = resp.json()["attachment_id"]
    root = await must_get_workflow_attachment(aid)
    assert root["active_sibling_id"] == new_id


async def test_empty_metadata_passes_empty_dict(client):
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "x", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"O", "workflow_id": "img"},
    )
    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(params)
        return b"N"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert captured == [{}]


async def test_malformed_metadata_falls_back_to_empty_dict(client):
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "x", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"O", "workflow_id": "img"},
    )
    # insert_workflow_attachment_row rejects non-dict metadata, so reach
    # past it to seed a string the production-path JSON parser will choke on.
    from backend.database.connection import get_db

    async with get_db() as conn:
        await conn.execute(
            "UPDATE workflow_attachments SET generation_metadata = ? WHERE id = ?",
            ("not-json{{", aid),
        )
        await conn.commit()

    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(params)
        return b"N"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert captured == [{}]


async def test_hook_raise_returns_500_and_no_insert(client):
    cid, mid, aid = await _seed_with_metadata(client)

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
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 500
    from backend.database import get_workflow_attachments_for_message

    rows = await get_workflow_attachments_for_message(mid)
    assert len(rows) == 1


async def test_hook_returns_non_bytes_500(client):
    cid, mid, aid = await _seed_with_metadata(client)

    async def reroll(ctx, params, seed):
        return "not bytes"  # type: ignore[return-value]

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 500


async def test_hook_returns_empty_bytes_500(client):
    cid, mid, aid = await _seed_with_metadata(client)

    async def reroll(ctx, params, seed):
        return b""

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 500
