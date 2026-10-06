"""Tests for `POST .../workflow-attachments/{aid}/reroll-gen`.

Pins the reroll-gen contract: the new sibling inherits the original's generation_metadata verbatim while the hook is invoked
with a freshly minted seed, so an evict-then-rehydrate cycle on the sibling reproduces its output deterministically.
"""

import asyncio
import json

import pytest

from backend.database import get_workflow_attachments_for_message, insert_workflow_attachment_row
from backend.database.connection import get_db
from backend.workflows.errors import WorkflowUserFacingError

from ._fixtures import (
    attachment_action,
    make_workflow,
    must_get_workflow_attachment,
    register_for_test,
    reroll_workflow,
    returning,
    seed_attachment,
)


async def _reroll(client, ids, value=b"N", calls=None, **body):
    with reroll_workflow(returning(value, calls)):
        return await attachment_action(client, *ids, "reroll-gen", **body)


async def test_workflow_without_reroll_gen_hook_returns_404(client):
    ids = await seed_attachment(client)
    with register_for_test(make_workflow("img")):  # no reroll_gen
        assert (await attachment_action(client, *ids, "reroll-gen")).status_code == 404


async def test_happy_path_inserts_new_sibling_with_fresh_seed_and_same_params(client):
    ids = _, _, aid = await seed_attachment(client)
    calls: list = []
    response = await _reroll(client, ids, b"NEW_BYTES", calls)
    assert response.status_code == 200
    new_row = await must_get_workflow_attachment(response.json()["attachment_id"])
    assert new_row["parent_attachment_id"] == aid
    assert new_row["workflow_id"] == "img"
    assert json.loads(new_row["generation_metadata"]) == {"steps": 4, "source_text": "scene"}
    assert new_row["seed"] != "ORIG-SEED"
    assert isinstance(new_row["seed"], str) and len(new_row["seed"]) == 32
    assert calls == [({"steps": 4}, new_row["seed"])]
    # The dispatcher marks the new sibling active.
    assert (await must_get_workflow_attachment(aid))["active_sibling_id"] == new_row["id"]


@pytest.mark.parametrize("stored", [None, "not-json{{"])
async def test_empty_or_malformed_metadata_passes_empty_dict(client, stored):
    ids = await seed_attachment(client, content="x", seed=None, generation_metadata=None)
    if stored is not None:
        # insert_workflow_attachment_row rejects non-dict metadata, so reach past it.
        async with get_db() as conn:
            await conn.execute("UPDATE workflow_attachments SET generation_metadata = ? WHERE id = ?", (stored, ids[2]))
            await conn.commit()
    calls: list = []
    await _reroll(client, ids, calls=calls)
    assert [params for params, _ in calls] == [{}]


@pytest.mark.parametrize(
    "failure,status",
    [
        (RuntimeError("boom"), 500),
        ("not bytes", 500),
        # A provider rejection is relayed as the provider's own sentence; 502, since the backend Orb depends on did not deliver.
        (WorkflowUserFacingError("OpenRouter rejected the request (HTTP 400): User location is not supported."), 502),
    ],
)
async def test_a_failed_hook_writes_no_sibling(client, failure, status):
    ids = await seed_attachment(client)
    response = await _reroll(client, ids, failure)
    assert response.status_code == status
    if isinstance(failure, WorkflowUserFacingError):
        assert response.json()["detail"] == str(failure)
    assert len(await get_workflow_attachments_for_message(ids[1])) == 1


# -- caller-supplied overrides ------------------------------------------------


@pytest.mark.parametrize(
    "stored,body,expected",
    [
        # The sibling recording the edit is what makes it stick: rerolling the sibling replays the edited prompt.
        ({"prompt": "original", "steps": 4}, {"params": {"prompt": "edited"}}, {"prompt": "edited", "steps": 4}),
        # Only keys the artifact already recorded, and only string-for-string.
        (
            {"prompt": "original", "steps": 4},
            {"params": {"unknown": "x", "steps": "9", "prompt": 5}},
            {"prompt": "original", "steps": 4},
        ),
        ({"prompt": "original"}, {"params": "edited"}, {"prompt": "original"}),  # a non-dict params body is ignored
    ],
)
async def test_overrides_reach_the_hook_and_land_in_the_new_sibling(client, stored, body, expected):
    ids = await seed_attachment(client, content="x", seed=None, generation_metadata=stored)
    calls: list = []
    response = await _reroll(client, ids, calls=calls, **body)
    assert response.status_code == 200
    recorded = json.loads((await must_get_workflow_attachment(response.json()["attachment_id"]))["generation_metadata"])
    assert recorded.pop("source_text") == "x"
    assert calls[0][0] == expected
    assert recorded == expected


async def test_late_reroll_keeps_user_variant_and_old_source(client):
    cid, mid, aid = await seed_attachment(client)
    sibling = await insert_workflow_attachment_row(
        mid,
        {"filename": "chosen.png", "mime": "image/png", "data": b"chosen", "workflow_id": "img", "parent_attachment_id": aid},
    )
    base = f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}"
    await client.post(base + "/activate", json={"sibling_id": aid})
    entered, release = asyncio.Event(), asyncio.Event()

    async def reroll(ctx, params, seed):
        entered.set()
        await release.wait()
        return b"late result"

    with reroll_workflow(reroll):
        running = asyncio.create_task(client.post(base + "/reroll-gen", json={}))
        await entered.wait()
        await client.post_checked(base + "/activate", json={"sibling_id": sibling})
        await client.post_checked(
            f"/api/conversations/{cid}/messages/{mid}/edit", json={"content": "new source", "regenerate": False}
        )
        release.set()
        result = await running
    assert result.status_code == 200
    assert (await must_get_workflow_attachment(aid))["active_sibling_id"] == sibling
    new_row = await must_get_workflow_attachment(result.json()["attachment_id"])
    assert json.loads(new_row["generation_metadata"])["source_text"] == "scene"
