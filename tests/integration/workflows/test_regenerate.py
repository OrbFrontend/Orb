"""Tests for `POST /api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate`."""

import asyncio
import os
import tempfile
from types import SimpleNamespace

import pytest

from backend.api import deps
from backend.api.routes import workflows as routes
from backend.database import (
    add_message,
    get_conversation,
    get_workflow_attachments_for_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.inference import EndpointConfigError
from backend.workflows.attachment_cache import OVERSIZE_NO_METADATA_REASON
from backend.workflows.errors import WorkflowUserFacingError

from ._fixtures import (
    attachment_action,
    make_workflow,
    must_get_workflow_attachment,
    new_conversation,
    register_for_test,
    seed_message,
)

_STREAM = {"Accept": "text/event-stream"}


async def _seed_workflow_attachment(mid: int, *, wid: str = "wf", **row) -> int:
    base = {"filename": "x.bin", "mime": "application/octet-stream", "data": b"DATA", "workflow_id": wid}
    return await insert_workflow_attachment_row(mid, {**base, **row})


def _workflow(wid: str, regen=None, **hooks):
    hooks.setdefault("reroll_gen", lambda ctx, params, seed: b"")
    return register_for_test(make_workflow(wid, regenerate=regen or (lambda ctx, body: []), produces_artifacts=True, **hooks))


def _returning(*entries):
    async def regen(ctx, body):
        return list(entries)

    return regen


async def _post(client, cid, mid, aid, action="regenerate", headers=None, **body):
    return await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/{action}", json=body, headers=headers
    )


async def _regenerate(client, regen, wid: str = "img", *, headers=None, **body):
    """Seed one attachment of *wid*, regenerate it with *regen*; return (response, mid, aid)."""
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid=wid)
    with _workflow(wid, regen):
        return await _post(client, cid, mid, aid, headers=headers, **body), mid, aid


async def test_attachment_message_mismatch_returns_404(client):
    cid, mid = await seed_message(client)
    other_mid, _ = await add_message(cid, "assistant", "other", 1, parent_id=mid)
    assert (await _post(client, cid, other_mid, (await _seed_workflow_attachment(mid)))).status_code == 404


async def test_workflow_without_regenerate_hook_returns_404(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="inert")
    with register_for_test(make_workflow("inert")):  # no regenerate hook
        assert (await attachment_action(client, cid, mid, aid, "regenerate")).status_code == 404


async def test_regenerate_inserts_returned_siblings_and_marks_the_last_active(client):
    regen = _returning(
        {"filename": "v1.png", "mime": "image/png", "data": b"V1"}, {"filename": "v2.png", "mime": "image/png", "data": b"V2"}
    )
    resp, _, aid = await _regenerate(client, regen)
    assert resp.status_code == 200
    new_ids = resp.json()["attachments"]
    assert len(new_ids) == 2
    for new_id in new_ids:
        row = await must_get_workflow_attachment(new_id)
        assert (row["parent_attachment_id"], row["workflow_id"]) == (aid, "img")
    assert (await must_get_workflow_attachment(aid))["active_sibling_id"] == new_ids[-1], "last sibling wins"


async def test_regenerate_ctx_history_excludes_the_anchor_and_after(client):
    cid = await new_conversation(client)
    # Build chain: m1 (user) -> m2 (assistant, anchor) -> m3 (user, after)
    m1, _ = await add_message(cid, "user", "u1", 0)
    m2, _ = await add_message(cid, "assistant", "anchor", 0, parent_id=m1)
    m3, _ = await add_message(cid, "user", "u3", 1, parent_id=m2)
    await set_active_leaf(cid, m3)
    root_cid, root_mid = await seed_message(client)  # the root anchor has no history at all
    captured: list = []

    async def regen(ctx, body):
        captured.append([m["id"] for m in ctx.history])
        return []

    with _workflow("hk", regen):
        for conv, anchor in ((cid, m2), (root_cid, root_mid)):
            assert (await _post(client, conv, anchor, (await _seed_workflow_attachment(anchor, wid="hk")))).status_code == 200
    assert captured == [[m1], []]


async def test_regenerate_hook_raise_returns_500_and_writes_nothing(client):
    async def regen(ctx, body):
        raise RuntimeError("boom")

    resp, mid, _ = await _regenerate(client, regen, "boom")
    assert resp.status_code == 500
    assert len(await get_workflow_attachments_for_message(mid)) == 1  # only the original


async def test_regenerate_hook_non_list_return_treated_as_empty(client):
    async def regen(ctx, body):
        return "not a list"  # type: ignore[return-value]

    resp, _, _ = await _regenerate(client, regen, "bad")
    assert resp.status_code == 200
    assert resp.json() == {"attachments": [], "rejected_workflow_atts": []}


@pytest.mark.parametrize(
    "entries,landed,rejected",
    [
        # Non-dict entries are silently dropped pre-validator (no filename to attribute); dict-shape entries that fail
        # validation surface with a reason, without rolling back the batch.
        (
            [
                {"filename": "good.png", "mime": "image/png", "data": b"OK"},
                {"filename": "broken.png", "mime": "image/png"},  # missing data
                "not a dict",
                {"filename": "good2.png", "mime": "image/png", "data": b"OK2"},
            ],
            2,
            ("broken.png", "exactly one of 'data' or 'path' required"),
        ),
        (
            [
                {"filename": "good.png", "mime": "image/png", "data": b"OK"},
                {"filename": "blank.png", "mime": "image/png", "data": b""},
                {"filename": "good2.png", "mime": "image/png", "data": b"OK2"},
            ],
            2,
            ("blank.png", "data is empty"),
        ),
        # A nonexistent path inside the staging root, so the "does not exist" gate (not containment) rejects it.
        (
            [
                {"filename": "good.png", "mime": "image/png", "data": b"OK"},
                {
                    "filename": "missing.png",
                    "mime": "image/png",
                    "path": os.path.join(tempfile.gettempdir(), "orb-test-nonexistent-dir", "missing.png"),
                },
            ],
            1,
            ("missing.png", "path does not exist or is not a regular file"),
        ),
    ],
)
async def test_regenerate_rejects_bad_entries_and_inserts_the_others(client, entries, landed, rejected):
    resp, _, aid = await _regenerate(client, _returning(*entries))
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attachments"]) == landed
    [rej] = body["rejected_workflow_atts"]
    assert (rej["filename"], rej["reason"], rej["originating_attachment_id"]) == (*rejected, aid)


async def test_regenerate_surfaces_rejected_atts_when_oversize_no_metadata(client, db):
    """An oversize return without seed+generation_metadata is dropped by the cache and surfaced as a rejection."""
    await db.execute("UPDATE settings SET attachment_cache_budget_bytes = 5 WHERE id = 1")  # trip the oversize gate
    await db.commit()
    regen = _returning(
        {"filename": "huge.png", "mime": "image/png", "data": b"H" * 100},  # non-rehydratable -> dropped
        {"filename": "rehydratable.png", "mime": "image/png", "data": b"R" * 100, "seed": "s", "generation_metadata": {}},
    )
    resp, _, aid = await _regenerate(client, regen, "drop")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["attachments"]) == 1, "rehydratable lands as marker"
    [rej] = body["rejected_workflow_atts"]
    assert (rej["filename"], rej["reason"], rej["originating_attachment_id"]) == ("huge.png", OVERSIZE_NO_METADATA_REASON, aid)


async def test_regenerate_passes_body_to_hook(client):
    captured: list[dict] = []

    async def regen(ctx, body):
        captured.append(body)
        return []

    await _regenerate(client, regen, "echo", hello="world")
    assert captured == [{"hello": "world"}]


@pytest.mark.parametrize(
    "entry", [{"filename": "n.png", "mime": "image/png", "data": b"N"}, {"filename": "broken.png", "mime": "image/png"}]
)
async def test_regenerating_a_sibling_anchors_on_the_root(client, entry):
    """New siblings point at the root, and a rejection chip anchors to the variant group, never the clicked sibling."""
    cid, mid = await seed_message(client)
    root_id = await _seed_workflow_attachment(mid, wid="flat")
    sibling_id = await _seed_workflow_attachment(mid, wid="flat", filename="sib", data=b"S", parent_attachment_id=root_id)
    with _workflow("flat", _returning(entry)):
        body = (await _post(client, cid, mid, sibling_id)).json()
    if "data" in entry:
        assert (await must_get_workflow_attachment(body["attachments"][0]))["parent_attachment_id"] == root_id
    else:
        assert [rej["originating_attachment_id"] for rej in body["rejected_workflow_atts"]] == [root_id]


@pytest.mark.parametrize(
    ("error", "status"), [(WorkflowUserFacingError("refused"), 502), (EndpointConfigError("refused"), 409)]
)
async def test_streamed_regenerate_failure_carries_the_status(client, error, status):
    async def regen(ctx, body):
        raise error

    resp, _, _ = await _regenerate(client, regen, "refused", headers=_STREAM)
    assert resp.text == f'event: regenerate_error\ndata: {{"status":{status},"detail":"refused"}}\n\n'


async def test_streamed_regenerate_lands_the_sibling_after_the_client_drops(client):
    cid, mid = await seed_message(client)
    aid = await _seed_workflow_attachment(mid, wid="dropped")
    release = asyncio.Event()

    async def regen(ctx, body):
        ctx.phase("Rendering...")
        await release.wait()
        return [{"filename": "v.png", "mime": "image/png", "data": b"V"}]

    request = SimpleNamespace(headers={"accept": "text/event-stream"})
    with _workflow("dropped", regen):
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

    with _workflow("stopped", regen):
        request = asyncio.create_task(_post(client, cid, mid, aid, headers=_STREAM))
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

    with _workflow("rerolled", reroll_gen=reroll):
        request = asyncio.create_task(_post(client, cid, mid, aid, "reroll-gen"))
        await asyncio.wait_for(started.wait(), 5)
        await client.post(f"/api/conversations/{cid}/workflows/stop")
        resp = await asyncio.wait_for(request, 5)
    assert resp.status_code == 409
    assert len(await get_workflow_attachments_for_message(mid)) == 1


async def test_stop_by_job_cancels_only_that_render(client):
    """A named Stop leaves the conversation's other renders running; a trigger (speech) is a render like any other."""
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

    with _workflow("twojobs", reroll_gen=reroll, on_demand=speak):
        rerolled = asyncio.create_task(_post(client, cid, mid, aid, "reroll-gen?job=a"))
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
    """Cancelling an await does not stop SQLite's worker thread, so a job inside its write is waited for."""
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


async def test_streamed_regenerate_forwards_only_the_workflows_own_events(client):
    """A hook's own status events reach the client; a name outside its prefix, or one the stream itself speaks, is dropped."""

    async def regen(ctx, body):
        ctx.emit("emitter_stage", {"n": 1})
        ctx.emit("other_stage", {"n": 2})
        ctx.emit("phase_status", {"label": "spoofed"})
        ctx.emit("emitter_bad", {"n": float("nan")})
        return []

    resp, _, _ = await _regenerate(client, regen, "emitter", headers=_STREAM)
    assert resp.text == (
        'event: emitter_stage\ndata: {"n":1}\n\n'
        'event: regenerate_done\ndata: {"attachments":[],"rejected_workflow_atts":[]}\n\n'
    )
