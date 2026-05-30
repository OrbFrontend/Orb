"""Pins per-(cid, workflow_id) serialization of ``/trigger``.

``workflow_state_lock(cid, wid)`` must serialize same-pair callers so a
read-modify-write hook never loses an increment, and must not serialize
different-pair callers so unrelated workflows on the same conversation
run in parallel.
"""

from __future__ import annotations

import asyncio
import time

from backend.database import get_workflow_state

from ._fixtures import (
    counter_on_demand_hook,
    make_workflow,
    register_for_test,
)


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "trigger-conc"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_n_concurrent_triggers_no_lost_writes(client):
    cid = await _new_conversation(client)
    wid = "counter_wf"

    wf = make_workflow(wid, on_demand=counter_on_demand_hook(wid, "n"))

    with register_for_test(wf):
        n = 20
        results = await asyncio.gather(
            *[client.post(f"/api/conversations/{cid}/workflows/{wid}/trigger", json={}) for _ in range(n)]
        )

    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    state = await get_workflow_state(cid, wid)
    assert state == {"n": n}, f"expected counter={n} after {n} concurrent triggers, got {state}"


async def test_different_workflow_ids_run_in_parallel(client):
    """Two ``/trigger``s on the same conversation but distinct workflow ids
    must not serialize against each other. Two hooks each sleep 0.3s; wall
    time staying well under the serialized total proves parallel execution.
    """
    cid = await _new_conversation(client)

    async def slow_hook_a(_ctx, _body):
        await asyncio.sleep(0.3)
        return {}

    async def slow_hook_b(_ctx, _body):
        await asyncio.sleep(0.3)
        return {}

    wf_a = make_workflow("wf_a", on_demand=slow_hook_a)
    wf_b = make_workflow("wf_b", on_demand=slow_hook_b)

    with register_for_test(wf_a), register_for_test(wf_b):
        start = time.perf_counter()
        results = await asyncio.gather(
            client.post(f"/api/conversations/{cid}/workflows/wf_a/trigger", json={}),
            client.post(f"/api/conversations/{cid}/workflows/wf_b/trigger", json={}),
        )
        elapsed = time.perf_counter() - start

    assert all(r.status_code == 200 for r in results)
    assert elapsed < 0.45, f"expected parallel execution under 0.45s, took {elapsed:.3f}s"
