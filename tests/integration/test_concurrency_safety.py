"""Controlled admission and persistence races; every interleaving uses a gate."""

import asyncio
import contextlib

import pytest
from fastapi import HTTPException

from backend.api import deps
from backend.database import get_conversation, get_message_by_id
from backend.inference import AbortToken


async def test_delete_waits_for_media_and_closes_admission(client):
    cid = await client.create("/api/conversations", json={})
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
    assert (await deleting).status_code == 200
    assert await get_conversation(cid) is None
    with contextlib.suppress(asyncio.CancelledError):
        await job
    assert not deps.resource_deleting(cid)


async def test_busy_card_and_cluster_resolution_leave_every_card(client):
    cards = [await client.create("/api/characters", json={"name": name}) for name in ["Keep", "First", "Busy"]]
    cid = await client.create("/api/conversations", json={"character_card_id": cards[2]})
    job = deps.start_workflow_job(cid, asyncio.Event().wait(), job="earlier-speaker-image")
    try:
        assert (await client.delete(f"/api/characters/{cards[2]}")).status_code == 409
        await client.post_checked(
            "/api/library/duplicates/resolve-group",
            json={"keep_id": cards[0], "remove_ids": cards[1:], "relink": True},
            expected_status=409,
        )
        assert all([(await client.get(f"/api/characters/{card}")).status_code == 200 for card in cards])
        assert (await get_conversation(cid))["character_card_id"] == cards[2]
    finally:
        job.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await job


async def test_cluster_resolution_rolls_back_if_a_later_member_fails(client, monkeypatch):
    from backend.database.queries import library_dedupe

    cards = [await client.create("/api/characters", json={"name": name}) for name in ["Keeper", "First", "Second"]]
    cids = [await client.create("/api/conversations", json={"character_card_id": card}) for card in cards[1:]]
    original = library_dedupe._relink_card_in_tx

    async def fail_later(db, source, keeper):
        if source == cards[2]:
            raise ValueError("The second card no longer passes review")
        return await original(db, source, keeper)

    monkeypatch.setattr(library_dedupe, "_relink_card_in_tx", fail_later)
    await client.post_checked(
        "/api/library/duplicates/resolve-group",
        json={"keep_id": cards[0], "remove_ids": cards[1:], "relink": True},
        expected_status=409,
    )
    assert all([(await client.get(f"/api/characters/{card}")).status_code == 200 for card in cards])
    for cid, card in zip(cids, cards[1:], strict=True):
        assert (await get_conversation(cid))["character_card_id"] == card
        assert not deps.resource_deleting(cid)


async def test_restore_refuses_admitted_write_and_old_epoch_cannot_save(client, monkeypatch):
    from backend.api.routes import documents
    from backend.features.presets import ALL_DOMAINS

    doc = await client.post_json("/api/documents", json={})
    path = f"/api/documents/{doc['id']}"
    epoch = (await client.get(path)).headers["X-Orb-Epoch"]
    name = (await client.post_json("/api/presets/export", json={"domains": list(ALL_DOMAINS)}))["name"]
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
    assert (await client.post_json(f"/api/presets/{name}/restore", expected_status=409))["detail"]["work"]
    release.set()
    assert (await saving).status_code == 200
    assert (await client.post_checked(f"/api/presets/{name}/restore")).headers["X-Orb-Epoch"] != epoch
    stale = await client.put_json(
        path, headers={"X-Orb-Epoch": epoch}, json={"content": "late", "expected_revision": 0}, expected_status=409
    )
    assert stale["detail"]["code"] == "refresh_required"
    assert (await client.get_json(path))["content"] == ""


async def test_stale_stop_does_not_abort_new_reply(client):
    cid = await client.create("/api/conversations", json={})
    token = AbortToken()
    active = await deps.work_registry.begin_stream(cid, token, "new-run")
    try:
        stale = await client.post(f"/api/conversations/{cid}/stop?operation_id=old-run")
        assert stale.json()["settled"] is True
        assert stale.json()["active"] is False
        assert not token.is_aborted
        active.settled.set()
        assert (await client.post(f"/api/conversations/{cid}/stop?operation_id=new-run")).json()["active"] is True
        assert token.is_aborted
    finally:
        deps.work_registry.finish_stream(cid, active)


async def test_message_delete_settles_only_its_source_jobs(client):
    from backend.database import add_message

    cid = await client.create("/api/conversations", json={})
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


@pytest.mark.parametrize("operation", ["delete_card", "relink_cards"])
async def test_bulk_writes_refuse_an_existing_deletion_fence_without_claiming_other_chats(client, operation):
    cards = [await client.create("/api/characters", json={"name": name}) for name in ["Keep", "Remove"]]
    chats = [await client.create("/api/conversations", json={"character_card_id": card}) for card in cards]
    # Deletion closes admission before it takes the lane. This is the window the
    # old bulk guard missed: no stream, render or held lane, but already deleting.
    with deps.work_registry.deletion_fence([chats[1]]):
        with pytest.raises(HTTPException) as exc, deps.idle_chats_guard([{"id": cid} for cid in chats]):
            pytest.fail("A bulk write borrowed an existing deletion fence")
        assert exc.value.status_code == 409
        assert not deps.resource_deleting(chats[0])
        assert deps.resource_deleting(chats[1])
        if operation == "delete_card":
            response = await client.delete(f"/api/characters/{cards[1]}?delete_conversations=true")
        else:
            response = await client.post(
                "/api/library/duplicates/resolve-group",
                json={"keep_id": cards[0], "remove_ids": [cards[1]], "relink": True},
            )
        assert response.status_code == 409
        assert deps.resource_deleting(chats[1]), "a refused write released another deletion's fence"
        assert (await get_conversation(chats[1]))["character_card_id"] == cards[1]
        assert all([(await client.get(f"/api/characters/{card}")).status_code == 200 for card in cards])
    assert not deps.resource_deleting(chats[1])


async def test_stopped_stream_remains_busy_until_settlement_and_lane_release():
    from backend.api.admission import dataset_maintenance
    from backend.api.work_registry import WorkPhase

    cid = "settling-stream"
    active = await deps.work_registry.begin_stream(cid, AbortToken(), "run")
    try:
        assert await deps.stop_active_stream(cid, timeout=0) == {"active": True, "settled": False}
        assert deps.work_registry.snapshot(cid).phases == {WorkPhase.SETTLING}
        assert deps.conversation_busy(cid)
        assert deps.active_work() == [f"Settling work in {cid}"]
        with pytest.raises(HTTPException), deps.idle_chats_guard([{"id": cid}]):
            pytest.fail("Bulk write was admitted before stream settlement")
        async with deps.stream_idle_lock(cid) as idle:
            assert not idle
        with pytest.raises(HTTPException):
            async with dataset_maintenance():
                pytest.fail("Restore was admitted before stream settlement")
    finally:
        deps.work_registry.finish_stream(cid, active)
    assert active.settled.is_set()
    assert not deps.work_registry.snapshot(cid).busy
    assert not deps.active_work()


async def test_repeated_media_stop_waits_for_cleanup_and_keeps_bulk_writes_out():
    from backend.api.work_registry import WorkPhase

    cid = "settling-render"
    started, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def render():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    task = deps.start_workflow_job(cid, render(), job="picture", message_id=42)
    await started.wait()
    try:
        assert await deps.stop_workflow_jobs(cid, timeout=0) == {"stopped": 1, "settled": False}
        await cleaning.wait()
        assert await deps.stop_workflow_jobs(cid, timeout=0) == {"stopped": 1, "settled": False}
        assert not task.done(), "repeated Stop interrupted the render's cleanup"
        assert WorkPhase.SETTLING in deps.work_registry.snapshot(cid).phases
        assert not deps.conversation_busy(cid), "media cleanup does not hold the history lane"
        assert deps.workflow_message_busy(cid, 42)
        with pytest.raises(HTTPException), deps.idle_chats_guard([{"id": cid}]):
            pytest.fail("Bulk write was admitted while media could still save")
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert not deps.work_registry.snapshot(cid).busy


async def test_lane_handoff_keeps_queued_mutations_busy_and_idle_admission_never_queues():
    cid = "lane-handoff"
    active = await deps.work_registry.begin_stream(cid, AbortToken(), None)
    waiting, entered, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def mutate():
        waiting.set()
        async with deps.conversation_stream_lock(cid):
            entered.set()
            await release.wait()

    mutation = asyncio.create_task(mutate())
    await waiting.wait()
    deps.work_registry.finish_stream(cid, active)
    try:
        # The old owner has released the lock, but its queued successor has not
        # resumed yet. Both bulk writes and new streams must still refuse it.
        assert not entered.is_set()
        assert deps.conversation_busy(cid)
        with pytest.raises(HTTPException), deps.idle_chats_guard([{"id": cid}]):
            pytest.fail("Bulk write entered during lane handoff")

        async def try_admission():
            with pytest.raises(deps.ChatUnavailable):
                async with deps.idle_chat_admission(cid):
                    pytest.fail("Idle admission entered a queued lane")

        await asyncio.wait_for(try_admission(), 1)
        assert not mutation.done()
    finally:
        release.set()
        await mutation
    assert not deps.work_registry.snapshot(cid).busy


async def test_media_stop_then_stream_disconnect_preserves_remote_cleanup():
    from backend.api.work_registry import WorkPhase
    from backend.workflows import WorkflowEventStream

    cid = "media-disconnect"
    rendering, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def events():
        try:
            yield {"event": "phase_status", "data": {"label": "Rendering"}}
            rendering.set()
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    response = deps.workflow_event_stream_response(WorkflowEventStream(events=events()), cid=cid, job="picture")
    frames = response.body_iterator
    assert "Rendering" in await anext(frames)
    await rendering.wait()
    assert await deps.stop_workflow_jobs(cid, timeout=0) == {"stopped": 1, "settled": False}
    await cleaning.wait()
    disconnected = asyncio.create_task(frames.aclose())
    try:
        done, _ = await asyncio.wait({disconnected}, timeout=0.02)
        assert not done, "disconnect interrupted the stopped render's remote cleanup"
        assert deps.work_registry.snapshot(cid).phases == {WorkPhase.SETTLING}
        jobs = deps.work_registry.jobs(cid)
        assert jobs
        # A second request cancellation must not propagate through the cleanup
        # await and cancel the render task again.
        disconnected.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await disconnected
        assert deps.work_registry.jobs(cid) == jobs
    finally:
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await disconnected
        if remaining := deps.work_registry.jobs(cid):
            await asyncio.wait(remaining)
    assert not deps.work_registry.snapshot(cid).busy
    assert not deps.work_registry._resources, "settled work retained an idle resource record"


@pytest.mark.parametrize("guard", ["bulk", "resource", "message"])
async def test_deletion_wrappers_preserve_errors_from_the_guarded_operation(guard):
    problem = deps.ChatUnavailable("busy", "another-chat")
    with pytest.raises(deps.ChatUnavailable) as exc:
        if guard == "bulk":
            with deps.idle_chats_guard([{"id": "guarded-chat"}]):
                raise problem
        elif guard == "resource":
            async with deps.deleting_resources(["guarded-chat"]):
                raise problem
        else:
            async with deps.deleting_message_sources("guarded-chat", {42}):
                raise problem
    assert exc.value is problem
    assert not deps.resource_deleting("guarded-chat")
    assert not deps.message_deleting(42)


async def test_a_stop_during_commit_does_not_disable_later_cancellation():
    cid = "commit-then-render"
    saving, saved, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def render():
        with deps.committing_workflow_job():
            saving.set()
            await release.wait()
        saved.set()
        await asyncio.Event().wait()

    task = deps.start_workflow_job(cid, render())
    await saving.wait()
    try:
        assert await deps.stop_workflow_jobs(cid, timeout=0) == {"stopped": 1, "settled": False}
        assert not task.cancelling(), "Stop cancelled a protected write"
        release.set()
        await saved.wait()
        assert await deps.stop_workflow_jobs(cid, timeout=1) == {"stopped": 1, "settled": True}
        assert task.cancelled()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
