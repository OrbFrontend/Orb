"""Controlled admission and persistence races; every interleaving uses a gate."""

import asyncio
import contextlib

import pytest
from fastapi import HTTPException

from backend.api import deps
from backend.database import get_conversation, get_message_by_id
from backend.inference import AbortToken


async def test_delete_waits_for_media_and_closes_admission(client):
    cid = (await client.post("/api/conversations", json={})).json()["id"]
    started, winding_down, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def render():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            winding_down.set()
            await release.wait()
            raise

    job = deps.start_workflow_job(cid, render(), job="image")
    await started.wait()
    deleting = asyncio.create_task(client.delete(f"/api/conversations/{cid}"))
    await winding_down.wait()
    assert await get_conversation(cid) is not None
    with pytest.raises(HTTPException) as exc:
        deps.start_workflow_job(cid, render())
    assert exc.value.status_code == 409
    release.set()
    response = await deleting
    assert response.status_code == 200
    assert await get_conversation(cid) is None
    with contextlib.suppress(asyncio.CancelledError):
        await job
    assert cid not in deps._deleting_resources


async def test_busy_card_and_cluster_resolution_leave_every_card(client):
    cards = [(await client.post("/api/characters", json={"name": name})).json()["id"] for name in ["Keep", "First", "Busy"]]
    cid = (await client.post("/api/conversations", json={"character_card_id": cards[2]})).json()["id"]
    job = deps.start_workflow_job(cid, asyncio.Event().wait(), job="earlier-speaker-image")
    try:
        assert (await client.delete(f"/api/characters/{cards[2]}")).status_code == 409
        resolved = await client.post(
            "/api/library/duplicates/resolve-group",
            json={
                "keep_id": cards[0],
                "remove_ids": cards[1:],
                "relink": True,
            },
        )
        assert resolved.status_code == 409
        assert all([(await client.get(f"/api/characters/{card}")).status_code == 200 for card in cards])
        assert (await get_conversation(cid))["character_card_id"] == cards[2]
    finally:
        job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job


async def test_cluster_resolution_rolls_back_if_a_later_member_fails(client, monkeypatch):
    from backend.database.queries import library_dedupe

    cards = [(await client.post("/api/characters", json={"name": name})).json()["id"] for name in ["Keeper", "First", "Second"]]
    cids = [(await client.post("/api/conversations", json={"character_card_id": card})).json()["id"] for card in cards[1:]]
    original = library_dedupe._relink_card_in_tx

    async def fail_later(db, source, keeper):
        if source == cards[2]:
            raise ValueError("The second card no longer passes review")
        return await original(db, source, keeper)

    monkeypatch.setattr(library_dedupe, "_relink_card_in_tx", fail_later)
    result = await client.post(
        "/api/library/duplicates/resolve-group",
        json={"keep_id": cards[0], "remove_ids": cards[1:], "relink": True},
    )
    assert result.status_code == 409
    assert all([(await client.get(f"/api/characters/{card}")).status_code == 200 for card in cards])
    for cid, card in zip(cids, cards[1:], strict=True):
        assert (await get_conversation(cid))["character_card_id"] == card
        assert cid not in deps._deleting_resources


async def test_restore_refuses_admitted_write_and_old_epoch_cannot_save(client, monkeypatch):
    from backend.api.routes import documents
    from backend.features.presets import ALL_DOMAINS

    doc = (await client.post("/api/documents", json={})).json()
    path = f"/api/documents/{doc['id']}"
    epoch = (await client.get(path)).headers["X-Orb-Epoch"]
    name = (await client.post("/api/presets/export", json={"domains": list(ALL_DOMAINS)})).json()["name"]
    entered, release = asyncio.Event(), asyncio.Event()
    original = documents.get_document

    async def gated(did):
        entered.set()
        await release.wait()
        return await original(did)

    monkeypatch.setattr(documents, "get_document", gated)
    saving = asyncio.create_task(
        client.put(path, headers={"X-Orb-Epoch": epoch}, json={"content": "saved", "expected_revision": 0})
    )
    await entered.wait()
    refused = await client.post(f"/api/presets/{name}/restore")
    assert refused.status_code == 409
    assert refused.json()["detail"]["work"]
    release.set()
    assert (await saving).status_code == 200
    restored = await client.post(f"/api/presets/{name}/restore")
    assert restored.status_code == 200
    assert restored.headers["X-Orb-Epoch"] != epoch
    stale = await client.put(path, headers={"X-Orb-Epoch": epoch}, json={"content": "late", "expected_revision": 0})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "refresh_required"
    assert (await client.get(path)).json()["content"] == ""


async def test_stale_stop_does_not_abort_new_reply(client):
    cid = (await client.post("/api/conversations", json={})).json()["id"]
    token = AbortToken()
    active = deps._ActiveStream(token, operation_id="new-run")
    deps._active_streams[cid] = active
    try:
        stale = await client.post(f"/api/conversations/{cid}/stop?operation_id=old-run")
        assert stale.json()["settled"] is True
        assert stale.json()["active"] is False
        assert not token.is_aborted
        active.settled.set()
        current = await client.post(f"/api/conversations/{cid}/stop?operation_id=new-run")
        assert current.json()["active"] is True
        assert token.is_aborted
    finally:
        deps._active_streams.pop(cid, None)


async def test_message_delete_settles_only_its_source_jobs(client):
    from backend.database import add_message

    cid = (await client.post("/api/conversations", json={})).json()["id"]
    other, _ = await add_message(cid, "user", "parent", 0)
    first, _ = await add_message(cid, "assistant", "first", 1, parent_id=other)
    entered = asyncio.Event()

    async def render():
        entered.set()
        await asyncio.Event().wait()

    source = deps.start_workflow_job(cid, render(), message_id=first)
    unrelated = deps.start_workflow_job(cid, asyncio.Event().wait(), message_id=other)
    await entered.wait()
    try:
        assert (await client.delete(f"/api/conversations/{cid}/messages/{first}")).status_code == 200
        assert source.cancelled()
        assert not unrelated.done()
        assert await get_message_by_id(first) is None
        assert await get_message_by_id(other) is not None
    finally:
        unrelated.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await unrelated
