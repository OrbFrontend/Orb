"""Pins the consumption_metadata contract: workflows write a dict, the row
helper JSON-encodes it (or silently stores NULL for non-dicts and
non-serializable dicts), and the bulk reader returns the stored JSON string
unchanged for the frontend to decode.
"""

from __future__ import annotations

import json

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.database.queries.messages import get_workflow_attachments_for_message
from backend.workflows.attachment_cache import EVICTED_MARKER, evict

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "cm"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_with_consumption_metadata(client, payload: dict | None) -> tuple[str, int, int]:
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
            "consumption_metadata": payload,
        },
    )
    return cid, mid, aid


async def test_consumption_metadata_round_trip_to_bulk_reader(client):
    cid, mid, _ = await _seed_with_consumption_metadata(client, {"cues": [0.5, 1.25]})
    rows = await get_workflow_attachments_for_message(mid)
    assert len(rows) == 1
    raw = rows[0]["consumption_metadata"]
    assert isinstance(raw, str)
    assert json.loads(raw) == {"cues": [0.5, 1.25]}


async def test_consumption_metadata_absent_stores_null(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, None)
    row = await must_get_workflow_attachment(aid)
    assert row["consumption_metadata"] is None


async def test_consumption_metadata_non_dict_stores_null(client):
    # The row helper accepts only dict values for the metadata fields;
    # non-dict input is silently coerced to NULL at the storage boundary.
    cid, mid, aid = await _seed_with_consumption_metadata(client, "not a dict")  # type: ignore[arg-type]
    row = await must_get_workflow_attachment(aid)
    assert row["consumption_metadata"] is None


async def test_consumption_metadata_non_serializable_stores_null_and_does_not_raise(client):
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"X",
            "workflow_id": "img",
            "consumption_metadata": {"bad": {1, 2, 3}},
        },
    )
    row = await must_get_workflow_attachment(aid)
    assert row["consumption_metadata"] is None


async def test_generation_metadata_non_serializable_stores_null_and_does_not_raise(client):
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"X",
            "workflow_id": "img",
            "generation_metadata": {"bad": {1, 2, 3}},
        },
    )
    row = await must_get_workflow_attachment(aid)
    assert row["generation_metadata"] is None


async def test_reroll_gen_tuple_return_writes_fresh_consumption_metadata(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"v": 1})

    async def reroll(ctx, params, seed):
        return (b"NEW", {"v": 2})

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
    assert json.loads(new_row["consumption_metadata"]) == {"v": 2}


async def test_reroll_gen_raw_bytes_writes_null_consumption_metadata(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"v": 1})

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
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 200
    new_id = resp.json()["attachment_id"]
    new_row = await must_get_workflow_attachment(new_id)
    assert new_row["consumption_metadata"] is None


async def test_reroll_gen_tuple_with_non_dict_metadata_coerces_to_null(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"v": 1})

    async def reroll(ctx, params, seed):
        return (b"NEW", "not a dict")

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
    assert new_row["consumption_metadata"] is None


async def test_reroll_gen_hook_reads_prior_consumption_metadata(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"orig": True})
    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(ctx.prior_consumption_metadata)
        return b"NEW"

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
    assert len(captured) == 1
    prior = captured[0]
    assert prior is not None
    assert dict(prior) == {"orig": True}


async def test_reroll_gen_hook_prior_consumption_metadata_is_none_when_absent(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, None)
    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(ctx.prior_consumption_metadata)
        return b"NEW"

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
    assert captured == [None]


async def test_rehydrate_tuple_dict_overwrites_consumption_metadata(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"orig": True})
    await evict(aid)

    async def reroll(ctx, params, seed):
        return (b"RECOVERED", {"fresh": True})

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
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] != EVICTED_MARKER
    assert json.loads(row["consumption_metadata"]) == {"fresh": True}


async def test_rehydrate_raw_bytes_keeps_stored_consumption_metadata(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"orig": True})
    await evict(aid)

    async def reroll(ctx, params, seed):
        return b"RECOVERED"

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
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] != EVICTED_MARKER
    assert json.loads(row["consumption_metadata"]) == {"orig": True}


async def test_rehydrate_tuple_none_keeps_stored_consumption_metadata(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"orig": True})
    await evict(aid)

    async def reroll(ctx, params, seed):
        return (b"RECOVERED", None)

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
    row = await must_get_workflow_attachment(aid)
    assert json.loads(row["consumption_metadata"]) == {"orig": True}


async def test_rehydrate_tuple_non_dict_coerces_and_keeps_stored(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, {"orig": True})
    await evict(aid)

    async def reroll(ctx, params, seed):
        return (b"RECOVERED", "not a dict")

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
    row = await must_get_workflow_attachment(aid)
    assert json.loads(row["consumption_metadata"]) == {"orig": True}


async def test_rehydrate_overwrites_when_stored_was_null(client):
    cid, mid, aid = await _seed_with_consumption_metadata(client, None)
    await evict(aid)

    async def reroll(ctx, params, seed):
        return (b"RECOVERED", {"new": 1})

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
    row = await must_get_workflow_attachment(aid)
    assert json.loads(row["consumption_metadata"]) == {"new": 1}
