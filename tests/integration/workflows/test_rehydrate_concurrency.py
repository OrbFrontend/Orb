"""Pins per-root serialization of ``/rehydrate``.

Two concurrent rehydrate requests against the same evicted root must
invoke the workflow's ``reroll_gen`` hook exactly once: the first acquires
``_workflow_root_lock(root_id)`` and runs the regen; the second waits,
acquires after the first releases, re-reads the row inside the lock,
observes that ``data_b64`` is no longer the eviction sentinel, and 409s
without spending a second LLM call. The bytes-write race is also closed
by the cache helper's ``BEGIN IMMEDIATE`` recheck downstream; the in-lock
re-read in the handler is what prevents double work on the upstream side.
"""

from __future__ import annotations

import asyncio
import base64

from backend.database import (
    add_message,
    get_workflow_attachment_by_id,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.workflows.attachment_cache import EVICTED_MARKER, evict

from ._fixtures import make_workflow, register_for_test


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "rehydrate-conc"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_evicted(client) -> tuple[str, int, int]:
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
            "seed": "STORED-SEED",
            "generation_metadata": {"steps": 4},
        },
    )
    await evict(aid)
    return cid, mid, aid


async def test_two_concurrent_rehydrates_hook_runs_once(client):
    cid, mid, aid = await _seed_evicted(client)

    call_count = 0
    in_hook = asyncio.Event()
    release_hook = asyncio.Event()

    async def reroll(ctx, params, seed):
        nonlocal call_count
        call_count += 1
        in_hook.set()
        await release_hook.wait()
        return b"RECOVERED"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )

    async def post():
        return await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate",
            json={},
        )

    with register_for_test(wf):
        first_task = asyncio.create_task(post())
        await in_hook.wait()
        second_task = asyncio.create_task(post())
        await asyncio.sleep(0.05)
        release_hook.set()
        first_resp, second_resp = await asyncio.gather(first_task, second_task)

    statuses = sorted([first_resp.status_code, second_resp.status_code])
    assert statuses == [200, 409], f"expected one 200 + one 409, got {statuses}"
    assert call_count == 1, f"reroll_gen ran {call_count} times; lock should serialize to 1"

    row = await get_workflow_attachment_by_id(aid)
    assert row is not None
    assert row["data_b64"] != EVICTED_MARKER
    assert base64.b64decode(row["data_b64"]) == b"RECOVERED"
