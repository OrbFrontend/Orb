"""SSE projection test for `workflow_attachments_rejected` event.

The orchestrator's `_consume_pipeline` emits a
`workflow_attachments_rejected` event after `_persist_result` when the
cache drops one or more workflow attachments for rehydratability
reasons. This test pins the SSE event shape -- specifically the
`reason` field whose presence the frontend chip depends on.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from backend.database import add_message, set_active_leaf
from backend.pipeline.orchestrator import _consume_pipeline
from backend.workflows.attachment_cache import OVERSIZE_NO_METADATA_REASON

from ._fixtures import make_workflow, register_for_test


@pytest.fixture(autouse=True)
def _register_sse_test_workflow():
    """Register ``"sse-test"`` with produces_artifacts=True so the cache's
    Step-0 policy partition admits the attachment instead of rejecting it
    upstream with WORKFLOW_NOT_PRODUCES_ARTIFACTS_REASON. The test needs
    the Step-A oversize rejection path, not the policy-partition path."""
    wf = make_workflow(
        "sse-test",
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(wf):
        yield


async def _seed(client) -> tuple[str, int]:
    resp = await client.post("/api/conversations", json={"title": "sse-reject"})
    assert resp.status_code == 200
    cid = resp.json()["id"]
    user_id, _ = await add_message(cid, "user", "ping", 0)
    await set_active_leaf(cid, user_id)
    return cid, user_id


async def _fake_pipeline(staged_atts: list[dict]) -> AsyncIterator[dict]:
    yield {
        "event": "_result",
        "data": {
            "active_moods": [],
            "resp_text": "assistant draft",
            "staged_attachments": staged_atts,
        },
    }
    yield {"event": "done", "data": {}}


async def test_sse_emits_workflow_attachments_rejected_with_reason(client, db):
    cid, user_id = await _seed(client)
    # Budget < attachment size combined with missing seed+generation_metadata
    # forces the Step-A oversize-no-metadata rejection path.
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = 5 WHERE id = 1")
    await db.commit()

    staged = [
        # Oversize, no seed + generation_metadata -> non-rehydratable -> rejected.
        {
            "filename": "huge.png",
            "mime": "image/png",
            "data": b"H" * 100,
            "source": "workflow:sse-test",
            "workflow_id": "sse-test",
        }
    ]
    settings = {"enable_agent": 0}
    events = [e async for e in _consume_pipeline(_fake_pipeline(staged), cid, settings, user_id, 1)]

    rejected_events = [e for e in events if e["event"] == "workflow_attachments_rejected"]
    assert len(rejected_events) == 1
    payload = rejected_events[0]["data"]
    assert isinstance(payload["message_id"], int)
    assert len(payload["rejected"]) == 1
    entry = payload["rejected"][0]
    assert entry["filename"] == "huge.png"
    assert entry["workflow_id"] == "sse-test"
    assert entry["mime"] == "image/png"
    assert entry["reason"] == OVERSIZE_NO_METADATA_REASON
    # SSE-path rejections never had a DB row, so there is no originating
    # attachment to point the frontend at; null tells the renderer to use
    # the message-level footer chip rather than a per-widget chip.
    assert entry["originating_attachment_id"] is None
    # Bytes/path/seed/generation_metadata must not leak into the SSE payload.
    assert "data" not in entry
    assert "path" not in entry
    assert "seed" not in entry
    assert "generation_metadata" not in entry
