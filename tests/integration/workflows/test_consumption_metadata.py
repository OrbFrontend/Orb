"""Pins the consumption_metadata contract: workflows write a dict, the row helper JSON-encodes it (or silently stores NULL for
non-dicts and non-serializable dicts), and the bulk reader returns the stored JSON string unchanged for the frontend to
decode.
"""

import json

import pytest

from backend.database import add_message, insert_workflow_attachment_row, set_active_leaf
from backend.database.queries.messages import get_workflow_attachments_for_message
from backend.workflows.attachment_cache import EVICTED_MARKER, evict

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


async def _seed(client, **row) -> tuple[str, int, int]:
    cid = await client.create("/api/conversations", json={"title": "cm"})
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    base = {"filename": "x.png", "mime": "image/png", "data": b"OG", "workflow_id": "img"}
    return cid, mid, await insert_workflow_attachment_row(mid, {**base, **row})


async def _seed_with_consumption_metadata(client, payload: dict | None) -> tuple[str, int, int]:
    return await _seed(client, seed="ORIG-SEED", generation_metadata={"steps": 4}, consumption_metadata=payload)


async def _replay(client, action: str, result, stored: dict | None, *, evicted: bool = False):
    """Run *action* against a row storing *stored*, with a reroll_gen hook returning *result*; return (row id, hook ctxs)."""
    cid, mid, aid = await _seed_with_consumption_metadata(client, stored)
    if evicted:
        await evict(aid)
    seen: list = []

    async def reroll(ctx, params, seed):
        seen.append(ctx)
        return result

    with register_for_test(make_workflow("img", regenerate=lambda ctx, body: [], reroll_gen=reroll, produces_artifacts=True)):
        resp = await client.post_json(f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/{action}", json={})
    return (resp["attachment_id"] if action == "reroll-gen" else aid), seen


def _decoded(row: dict):
    raw = row["consumption_metadata"]
    return None if raw is None else json.loads(raw)


async def test_consumption_metadata_round_trip_to_bulk_reader(client):
    _, mid, _ = await _seed_with_consumption_metadata(client, {"cues": [0.5, 1.25]})
    [row] = await get_workflow_attachments_for_message(mid)
    assert isinstance(row["consumption_metadata"], str)
    assert json.loads(row["consumption_metadata"]) == {"cues": [0.5, 1.25]}


@pytest.mark.parametrize(
    "column,value",
    [
        ("consumption_metadata", None),
        ("consumption_metadata", {"bad": {1, 2, 3}}),  # non-serializable: NULL, and no raise
        ("generation_metadata", {"bad": {1, 2, 3}}),
    ],
)
async def test_unusable_metadata_stores_null(client, column, value):
    _, _, aid = await _seed(client, **{column: value})
    assert (await must_get_workflow_attachment(aid))[column] is None


@pytest.mark.parametrize("result,expected", [((b"NEW", {"v": 2}), {"v": 2}), (b"NEW", None)])
async def test_reroll_gen_writes_the_hooks_fresh_consumption_metadata(client, result, expected):
    new_id, _ = await _replay(client, "reroll-gen", result, {"v": 1})
    assert _decoded(await must_get_workflow_attachment(new_id)) == expected


@pytest.mark.parametrize("stored", [{"orig": True}, None])
async def test_reroll_gen_hook_reads_prior_consumption_metadata(client, stored):
    _, [ctx] = await _replay(client, "reroll-gen", b"NEW", stored)
    prior = ctx.prior_consumption_metadata
    assert (dict(prior) if prior is not None else None) == stored


@pytest.mark.parametrize(
    "result,stored,expected",
    [
        ((b"RECOVERED", {"fresh": True}), {"orig": True}, {"fresh": True}),  # a tuple dict overwrites
        (b"RECOVERED", {"orig": True}, {"orig": True}),  # raw bytes keep the stored metadata
        ((b"RECOVERED", None), {"orig": True}, {"orig": True}),  # so does a tuple None
        ((b"RECOVERED", {"new": 1}), None, {"new": 1}),  # overwrites a stored NULL
    ],
)
async def test_rehydrate_consumption_metadata(client, result, stored, expected):
    aid, _ = await _replay(client, "rehydrate", result, stored, evicted=True)
    row = await must_get_workflow_attachment(aid)
    assert row["data_b64"] != EVICTED_MARKER
    assert _decoded(row) == expected
