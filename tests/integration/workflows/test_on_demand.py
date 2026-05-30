"""Tests for POST /api/conversations/{cid}/workflows/{workflow_id}/trigger.

Covers the on-demand dispatch route: 404 surfaces (smoke 19.6), happy-path
return value, body pass-through, and 500 isolation on hook raise.
"""

from __future__ import annotations

from ._fixtures import make_workflow, register_for_test


async def _make_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "On-demand test"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_unregistered_workflow_returns_404(client):
    cid = await _make_conversation(client)
    resp = await client.post(
        f"/api/conversations/{cid}/workflows/no-such-workflow/trigger",
        json={},
    )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Workflow 'no-such-workflow' is not registered"}


async def test_workflow_without_on_demand_hook_returns_404(client):
    cid = await _make_conversation(client)
    wf = make_workflow("inert", display_name="Inert")
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/workflows/inert/trigger",
            json={},
        )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Workflow 'inert' is not registered"}


async def test_missing_conversation_returns_404(client):
    async def on_demand(ctx, payload):
        return {"ok": True}

    wf = make_workflow("registered", display_name="Registered", on_demand=on_demand)
    with register_for_test(wf):
        resp = await client.post(
            "/api/conversations/no-such-conv/workflows/registered/trigger",
            json={},
        )
    assert resp.status_code == 404
    assert resp.json() == {"detail": "Conversation not found"}


async def test_happy_path_returns_hook_payload(client):
    cid = await _make_conversation(client)

    async def on_demand(ctx, payload):
        return {"ok": True, "echo": payload, "cid": ctx.conversation_id}

    wf = make_workflow("echo", display_name="Echo", on_demand=on_demand)
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/workflows/echo/trigger",
            json={"hello": "world"},
        )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "echo": {"hello": "world"}, "cid": cid}


async def test_empty_body_resolves_to_empty_dict(client):
    cid = await _make_conversation(client)
    captured: list[dict] = []

    async def on_demand(ctx, payload):
        captured.append(payload)
        return {"received": payload}

    wf = make_workflow("capture", display_name="Capture", on_demand=on_demand)
    with register_for_test(wf):
        resp = await client.post(f"/api/conversations/{cid}/workflows/capture/trigger")
    assert resp.status_code == 200
    assert captured == [{}]


async def test_hook_raise_returns_500_and_isolated(client):
    cid = await _make_conversation(client)
    call_count = {"n": 0}

    async def on_demand(ctx, payload):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated failure")
        return {"recovered": True}

    wf = make_workflow("flaky", display_name="Flaky", on_demand=on_demand)
    with register_for_test(wf):
        bad = await client.post(f"/api/conversations/{cid}/workflows/flaky/trigger", json={})
        assert bad.status_code == 500
        assert bad.json() == {"detail": "On-demand handler raised; see server logs"}

        good = await client.post(f"/api/conversations/{cid}/workflows/flaky/trigger", json={})
        assert good.status_code == 200
        assert good.json() == {"recovered": True}
