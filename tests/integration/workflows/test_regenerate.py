"""Tests for `POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate`."""

from __future__ import annotations

import asyncio
import os
import tempfile
from types import SimpleNamespace

from backend.api import deps
from backend.api.routes import workflows as routes
from backend.database import (
    add_message,
    get_conversation,
    get_workflow_attachments_for_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.workflows.attachment_cache import OVERSIZE_NO_METADATA_REASON
from backend.workflows.errors import WorkflowUserFacingError

from ._fixtures import (
    make_workflow,
    must_get_workflow_attachment,
    new_conversation,
    register_for_test,
    seed_message,
)


async def _seed_workflow_attachment(mid: int, *, wid: str = "wf") -> int:
    return await insert_workflow_attachment_row(
        mid,
        {"filename": "x.bin", "mime": "application/octet-stream", "data": b"DATA", "workflow_id": wid},
    )


async def test_attachment_message_mismatch_returns_404(client):
    cid, mid = await seed_message(client)
    other_mid, _ = await add_message(cid, "assistant", "other", 1, parent_id=mid)
    aid = await _seed_workflow_attachment(mid)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{other_mid}/workflow-attachments/{aid}/regenerate",
        json={},
    )
    assert resp.status_code == 404


async def test_workflow_without_regenerate_hook_returns_404(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="inert")
    wf = make_workflow("inert")  # no regenerate hook
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 404


async def test_regenerate_inserts_returned_siblings(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="img")

    async def regen(ctx, body):
        return [
            {"filename": "v1.png", "mime": "image/png", "data": b"V1"},
            {"filename": "v2.png", "mime": "image/png", "data": b"V2"},
        ]

    wf = make_workflow(
        "img",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attachments"]) == 2
    for new_id in body["attachments"]:
        row = await must_get_workflow_attachment(new_id)
        assert row["parent_attachment_id"] == aid
        assert row["workflow_id"] == "img"


async def test_regenerate_dispatcher_marks_active_sibling(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="img")

    async def regen(ctx, body):
        return [
            {"filename": "v1.png", "mime": "image/png", "data": b"V1"},
            {"filename": "v2.png", "mime": "image/png", "data": b"V2"},
        ]

    wf = make_workflow(
        "img",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    new_ids = resp.json()["attachments"]
    root = await must_get_workflow_attachment(aid)
    assert root["active_sibling_id"] == new_ids[-1], "last sibling wins"


async def test_regenerate_ctx_history_excludes_anchor_message(client):
    cid = await new_conversation(client)
    # Build chain: m1 (user) -> m2 (assistant, anchor) -> m3 (user, after)
    m1, _ = await add_message(cid, "user", "u1", 0)
    m2, _ = await add_message(cid, "assistant", "anchor", 0, parent_id=m1)
    m3, _ = await add_message(cid, "user", "u3", 1, parent_id=m2)
    await set_active_leaf(cid, m3)
    aid = await _seed_workflow_attachment(m2, wid="hk")
    captured: list = []

    async def regen(ctx, body):
        captured.append([m["id"] for m in ctx.history])
        return []

    wf = make_workflow(
        "hk",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{m2}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    assert captured == [[m1]]


async def test_regenerate_ctx_history_empty_when_anchor_is_root(client):
    cid, mid = await seed_message(client)  # mid is the root (no parent)
    aid = await _seed_workflow_attachment(mid, wid="hk")
    captured: list = []

    async def regen(ctx, body):
        captured.append(list(ctx.history))
        return []

    wf = make_workflow(
        "hk",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert captured == [[]]


async def test_regenerate_hook_raise_returns_500_and_writes_nothing(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="boom")

    async def regen(ctx, body):
        raise RuntimeError("boom")

    wf = make_workflow(
        "boom",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 500
    from backend.database import get_workflow_attachments_for_message

    rows = await get_workflow_attachments_for_message(mid)
    assert len(rows) == 1  # only the original


async def test_regenerate_hook_non_list_return_treated_as_empty(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="bad")

    async def regen(ctx, body):
        return "not a list"  # type: ignore[return-value]

    wf = make_workflow(
        "bad",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    assert resp.json() == {"attachments": [], "rejected_workflow_atts": []}


async def test_regenerate_skips_bad_dict_entries_and_inserts_others(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="mix")

    async def regen(ctx, body):
        return [
            {"filename": "good.png", "mime": "image/png", "data": b"OK"},
            {"filename": "broken.png", "mime": "image/png"},  # missing data
            "not a dict",
            {"filename": "good2.png", "mime": "image/png", "data": b"OK2"},
        ]

    wf = make_workflow(
        "mix",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attachments"]) == 2, "two good entries land"
    # Policy: non-dict entries are silently dropped pre-validator (no
    # filename to attribute); dict-shape entries that fail validation
    # surface with a reason.
    assert len(body["rejected_workflow_atts"]) == 1
    rej = body["rejected_workflow_atts"][0]
    assert rej["filename"] == "broken.png"
    assert rej["reason"] == "exactly one of 'data' or 'path' required"
    assert rej["originating_attachment_id"] == aid


async def test_regenerate_surfaces_rejected_atts_when_oversize_no_metadata(client, db):
    """An oversize hook return without seed+generation_metadata is dropped by the cache (not raised, not marker-inserted); the route surfaces it via ``rejected_workflow_atts`` instead."""
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="drop")
    # Set budget tiny so the new atts trip the oversize gate.
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = 5 WHERE id = 1")
    await db.commit()

    async def regen(ctx, body):
        return [
            # No seed/metadata -> non-rehydratable, must be dropped.
            {"filename": "huge.png", "mime": "image/png", "data": b"H" * 100},
            # Rehydratable -> marker-inserted.
            {
                "filename": "rehydratable.png",
                "mime": "image/png",
                "data": b"R" * 100,
                "seed": "s",
                "generation_metadata": {},
            },
        ]

    wf = make_workflow(
        "drop",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attachments"]) == 1, "rehydratable lands as marker"
    assert len(body["rejected_workflow_atts"]) == 1
    assert body["rejected_workflow_atts"][0]["filename"] == "huge.png"
    assert body["rejected_workflow_atts"][0]["reason"] == OVERSIZE_NO_METADATA_REASON
    assert body["rejected_workflow_atts"][0]["originating_attachment_id"] == aid


async def test_regenerate_per_entry_skip_on_empty_bytes_continues_with_valid_entries(client):
    """Validator-rejected entries do not roll back the batch insert; sibling good entries land alongside the rejection."""
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="empty")

    async def regen(ctx, body):
        return [
            {"filename": "good.png", "mime": "image/png", "data": b"OK"},
            {"filename": "blank.png", "mime": "image/png", "data": b""},
            {"filename": "good2.png", "mime": "image/png", "data": b"OK2"},
        ]

    wf = make_workflow(
        "empty",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attachments"]) == 2
    assert len(body["rejected_workflow_atts"]) == 1
    rej = body["rejected_workflow_atts"][0]
    assert rej["filename"] == "blank.png"
    assert rej["reason"] == "data is empty"
    assert rej["originating_attachment_id"] == aid


async def test_regenerate_per_entry_skip_on_unreadable_path_continues(client):
    """Path-entry rejection by the validator does not roll back the batch; sibling good entries land alongside the rejection."""
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="badpath")

    # Nonexistent path inside the staging root so the "does not exist" gate
    # (not the staging-root containment gate) is what rejects it.
    missing_path = os.path.join(tempfile.gettempdir(), "orb-test-nonexistent-dir", "missing.png")

    async def regen(ctx, body):
        return [
            {"filename": "good.png", "mime": "image/png", "data": b"OK"},
            {"filename": "missing.png", "mime": "image/png", "path": missing_path},
        ]

    wf = make_workflow(
        "badpath",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attachments"]) == 1
    assert len(body["rejected_workflow_atts"]) == 1
    rej = body["rejected_workflow_atts"][0]
    assert rej["filename"] == "missing.png"
    assert rej["reason"] == "path does not exist or is not a regular file"
    assert rej["originating_attachment_id"] == aid


async def test_regenerate_passes_body_to_hook(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="echo")
    captured: list[dict] = []

    async def regen(ctx, body):
        captured.append(body)
        return []

    wf = make_workflow(
        "echo",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={"hello": "world"},
        )
    assert captured == [{"hello": "world"}]


async def test_regenerate_on_sibling_uses_root_for_new_siblings(client):
    cid, mid = await seed_message(client)
    root_id = await _seed_workflow_attachment(mid, wid="flat")
    sibling_id = await insert_workflow_attachment_row(
        mid,
        {"filename": "sib", "mime": "image/png", "data": b"S", "workflow_id": "flat", "parent_attachment_id": root_id},
    )

    async def regen(ctx, body):
        return [{"filename": "n.png", "mime": "image/png", "data": b"N"}]

    wf = make_workflow(
        "flat",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{sibling_id}/regenerate",
            json={},
        )
    new_id = resp.json()["attachments"][0]
    row = await must_get_workflow_attachment(new_id)
    assert row["parent_attachment_id"] == root_id, "siblings always point at root, not at other siblings"


async def test_regenerate_on_sibling_tags_rejection_with_root_id(client):
    """Rejection projections carry the root_id, not the clicked sibling's id, so the chip is anchored to the variant group rather than to the specific sibling the user clicked."""
    cid, mid = await seed_message(client)
    root_id = await _seed_workflow_attachment(mid, wid="tagroot")
    sibling_id = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "sib",
            "mime": "image/png",
            "data": b"S",
            "workflow_id": "tagroot",
            "parent_attachment_id": root_id,
        },
    )

    async def regen(ctx, body):
        return [{"filename": "broken.png", "mime": "image/png"}]  # missing data

    wf = make_workflow(
        "tagroot",
        regenerate=regen,
        reroll_gen=lambda ctx, params, seed: b"",
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{sibling_id}/regenerate",
            json={},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["rejected_workflow_atts"]) == 1
    rej = body["rejected_workflow_atts"][0]
    assert rej["originating_attachment_id"] == root_id
    assert rej["originating_attachment_id"] != sibling_id


async def test_streamed_regenerate_failure_carries_the_status(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="refused")

    async def regen(ctx, body):
        raise WorkflowUserFacingError("refused")

    wf = make_workflow("refused", regenerate=regen, reroll_gen=lambda ctx, params, seed: b"", produces_artifacts=True)
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
            json={},
            headers={"Accept": "text/event-stream"},
        )
    assert resp.text == 'event: regenerate_error\ndata: {"status":502,"detail":"refused"}\n\n'


async def test_streamed_regenerate_lands_the_sibling_after_the_client_drops(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="dropped")
    release = asyncio.Event()

    async def regen(ctx, body):
        ctx.phase("Rendering...")
        await release.wait()
        return [{"filename": "v.png", "mime": "image/png", "data": b"V"}]

    wf = make_workflow("dropped", regenerate=regen, reroll_gen=lambda ctx, params, seed: b"", produces_artifacts=True)
    request = SimpleNamespace(headers={"accept": "text/event-stream"})
    with register_for_test(wf):
        frames = (await routes.api_regenerate_attachment(cid, mid, aid, request, {}, await get_conversation(cid))).body_iterator
        assert "Rendering..." in await anext(frames)
        await frames.aclose()  # the browser went away mid-render
        release.set()
        await asyncio.gather(*deps._workflow_jobs.get(cid, ()))
    assert len(await get_workflow_attachments_for_message(mid)) == 2


async def test_stop_cancels_a_streamed_regenerate_and_frees_its_group(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="stopped")
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def regen(ctx, body):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return []

    wf = make_workflow("stopped", regenerate=regen, reroll_gen=lambda ctx, params, seed: b"", produces_artifacts=True)
    with register_for_test(wf):
        request = asyncio.create_task(
            client.post(
                f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate",
                json={},
                headers={"Accept": "text/event-stream"},
            )
        )
        await asyncio.wait_for(started.wait(), 5)
        stop = await client.post(f"/api/conversations/{cid}/workflows/stop")
        resp = await asyncio.wait_for(request, 5)
    assert stop.json() == {"ok": True, "stopped": 1, "settled": True}
    assert cancelled.is_set()
    assert 'event: regenerate_error\ndata: {"status":409,"detail":"Stopped"}' in resp.text
    assert len(await get_workflow_attachments_for_message(mid)) == 1
    assert not deps.workflow_group_in_flight(aid)


async def test_stop_cancels_a_reroll_and_answers_409(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="rerolled")
    started = asyncio.Event()

    async def reroll(ctx, params, seed):
        started.set()
        await asyncio.Event().wait()
        return b"never"

    wf = make_workflow("rerolled", regenerate=lambda ctx, body: [], reroll_gen=reroll, produces_artifacts=True)
    with register_for_test(wf):
        request = asyncio.create_task(
            client.post(f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen", json={})
        )
        await asyncio.wait_for(started.wait(), 5)
        await client.post(f"/api/conversations/{cid}/workflows/stop")
        resp = await asyncio.wait_for(request, 5)
    assert resp.status_code == 409
    assert len(await get_workflow_attachments_for_message(mid)) == 1


async def test_stop_by_job_cancels_only_that_render(client):
    """Each render's own button stops it: a named Stop leaves the conversation's
    other renders running, and a trigger (speech) is a render like any other."""
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="twojobs")
    rerolling, speaking, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def reroll(ctx, params, seed):
        rerolling.set()
        await release.wait()
        return b"rerolled"

    async def speak(ctx, body):
        speaking.set()
        await asyncio.Event().wait()
        return {"attachment_id": None}

    wf = make_workflow("twojobs", regenerate=lambda ctx, body: [], reroll_gen=reroll, on_demand=speak, produces_artifacts=True)
    with register_for_test(wf):
        rerolled = asyncio.create_task(
            client.post(f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen?job=a", json={})
        )
        spoken = asyncio.create_task(client.post(f"/api/conversations/{cid}/workflows/twojobs/trigger?job=b", json={}))
        await asyncio.wait_for(asyncio.gather(rerolling.wait(), speaking.wait()), 5)
        stop = await client.post(f"/api/conversations/{cid}/workflows/stop?job=b")
        assert (await asyncio.wait_for(spoken, 5)).status_code == 409
        assert not rerolled.done()
        release.set()
        assert (await asyncio.wait_for(rerolled, 5)).status_code == 200
    assert stop.json() == {"ok": True, "stopped": 1, "settled": True}
    assert len(await get_workflow_attachments_for_message(mid)) == 2


async def test_stop_lets_a_render_that_is_already_saving_land():
    """Cancelling an await does not stop SQLite's worker thread, so a job inside
    its write is waited for rather than reported stopped while its row commits."""
    saving, release = asyncio.Event(), asyncio.Event()

    async def job():
        with deps.committing_workflow_job():
            saving.set()
            await release.wait()
        return "saved"

    task = deps.start_workflow_job("cid-saving", job())
    await saving.wait()
    stop = asyncio.create_task(deps.stop_workflow_jobs("cid-saving"))
    await asyncio.sleep(0)
    release.set()
    assert await stop == {"stopped": 1, "settled": True}
    assert task.result() == "saved"
