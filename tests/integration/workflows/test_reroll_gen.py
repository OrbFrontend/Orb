"""Tests for `POST .../workflow-attachments/{aid}/reroll-gen`.

Pins the reroll-gen contract: the new sibling inherits the original's
generation_metadata verbatim while the hook is invoked with a freshly
minted seed, so an evict-then-rehydrate cycle on the sibling reproduces
its output deterministically.
"""

from __future__ import annotations

import json

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.workflows.errors import WorkflowUserFacingError

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "reroll-gen"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_with_metadata(client) -> tuple[str, int, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "x.png",
            "mime": "image/png",
            "data": b"OG",
            "workflow_id": "img",
            "seed": "ORIG-SEED",
            "generation_metadata": {"steps": 4},
        },
    )
    return cid, mid, aid


async def test_unknown_conversation_returns_404(client):
    resp = await client.post(
        "/api/conversations/no-such/messages/1/workflow-attachments/1/reroll-gen",
        json={},
    )
    assert resp.status_code == 404


async def test_attachment_not_found_returns_404(client):
    cid = await _new_conversation(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/1/workflow-attachments/99999/reroll-gen",
        json={},
    )
    assert resp.status_code == 404


async def test_workflow_without_reroll_gen_hook_returns_404(client):
    cid, mid, aid = await _seed_with_metadata(client)
    wf = make_workflow("img")  # no reroll_gen
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 404


async def test_happy_path_inserts_new_sibling_with_fresh_seed_and_same_params(client):
    cid, mid, aid = await _seed_with_metadata(client)
    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append((dict(params), seed))
        return b"NEW_BYTES"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 200
    new_id = resp.json()["attachment_id"]
    new_row = await must_get_workflow_attachment(new_id)
    assert new_row["parent_attachment_id"] == aid
    assert new_row["workflow_id"] == "img"
    assert json.loads(new_row["generation_metadata"]) == {"steps": 4}
    assert new_row["seed"] != "ORIG-SEED"
    assert isinstance(new_row["seed"], str) and len(new_row["seed"]) == 32
    params_passed, seed_passed = captured[0]
    assert params_passed == {"steps": 4}
    assert seed_passed == new_row["seed"]


async def test_dispatcher_marks_active_sibling_to_new_id(client):
    cid, mid, aid = await _seed_with_metadata(client)

    async def reroll(ctx, params, seed):
        return b"B"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    new_id = resp.json()["attachment_id"]
    root = await must_get_workflow_attachment(aid)
    assert root["active_sibling_id"] == new_id


async def test_empty_metadata_passes_empty_dict(client):
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "x", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"O", "workflow_id": "img"},
    )
    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(params)
        return b"N"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert captured == [{}]


async def test_malformed_metadata_falls_back_to_empty_dict(client):
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "x", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"O", "workflow_id": "img"},
    )
    # insert_workflow_attachment_row rejects non-dict metadata, so reach
    # past it to seed a string the production-path JSON parser will choke on.
    from backend.database.connection import get_db

    async with get_db() as conn:
        await conn.execute(
            "UPDATE workflow_attachments SET generation_metadata = ? WHERE id = ?",
            ("not-json{{", aid),
        )
        await conn.commit()

    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(params)
        return b"N"

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert captured == [{}]


async def test_hook_raise_returns_500_and_no_insert(client):
    cid, mid, aid = await _seed_with_metadata(client)

    async def reroll(ctx, params, seed):
        raise RuntimeError("boom")

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 500
    from backend.database import get_workflow_attachments_for_message

    rows = await get_workflow_attachments_for_message(mid)
    assert len(rows) == 1


async def test_a_user_facing_hook_failure_is_relayed_not_swallowed(client):
    """The reroll button's half of the render-failure contract.

    A provider rejection reached the streaming path as the provider's own sentence
    and this route as "reroll_gen handler raised; see server logs" -- the same failed
    render reading two different ways depending on which button was pressed. 502
    rather than 500: the backend Orb depends on is what did not deliver.
    """
    cid, mid, aid = await _seed_with_metadata(client)
    said = "OpenRouter rejected the request (HTTP 400): Google AI Studio: User location is not supported."

    async def reroll(ctx, params, seed):
        raise WorkflowUserFacingError(said)

    wf = make_workflow("img", regenerate=lambda ctx, body: [], reroll_gen=reroll, produces_artifacts=True)
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 502
    assert resp.json()["detail"] == said

    # A failed reroll still costs the user nothing: no sibling was written.
    from backend.database import get_workflow_attachments_for_message

    assert len(await get_workflow_attachments_for_message(mid)) == 1


async def test_hook_returns_non_bytes_500(client):
    cid, mid, aid = await _seed_with_metadata(client)

    async def reroll(ctx, params, seed):
        return "not bytes"  # type: ignore[return-value]

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 500


async def test_hook_returns_empty_bytes_500(client):
    cid, mid, aid = await _seed_with_metadata(client)

    async def reroll(ctx, params, seed):
        return b""

    wf = make_workflow(
        "img",
        regenerate=lambda ctx, body: [],
        reroll_gen=reroll,
        produces_artifacts=True,
    )
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json={},
        )
    assert resp.status_code == 500


# ── caller-supplied overrides ────────────────────────────────────────────────


async def _reroll_with_overrides(client, stored: dict, body: dict) -> tuple[dict, dict]:
    """Reroll an attachment carrying `stored` params with `body`; return (params seen by
    the hook, params the new sibling recorded)."""
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "x", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"O", "workflow_id": "img", "generation_metadata": stored},
    )
    captured: list = []

    async def reroll(ctx, params, seed):
        captured.append(dict(params))
        return b"N"

    wf = make_workflow("img", regenerate=lambda ctx, body: [], reroll_gen=reroll, produces_artifacts=True)
    with register_for_test(wf):
        resp = await client.post(
            f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen",
            json=body,
        )
    assert resp.status_code == 200
    new_row = await must_get_workflow_attachment(resp.json()["attachment_id"])
    return captured[0], json.loads(new_row["generation_metadata"])


async def test_override_reaches_the_hook_and_lands_in_the_new_sibling(client):
    # The sibling recording the edit is what makes it stick: rerolling the sibling
    # replays the edited prompt with no further plumbing.
    seen, stored = await _reroll_with_overrides(
        client,
        {"prompt": "original", "steps": 4},
        {"params": {"prompt": "edited"}},
    )
    assert seen == {"prompt": "edited", "steps": 4}
    assert stored == {"prompt": "edited", "steps": 4}


async def test_overrides_cannot_invent_or_retype_params(client):
    # Only keys the artifact already recorded, and only string-for-string: a client may
    # retarget a render it can see, not hand the workflow parameters it never wrote.
    seen, stored = await _reroll_with_overrides(
        client,
        {"prompt": "original", "steps": 4},
        {"params": {"unknown": "x", "steps": "9", "prompt": 5}},
    )
    assert seen == {"prompt": "original", "steps": 4}
    assert stored == {"prompt": "original", "steps": 4}


async def test_a_non_dict_params_body_is_ignored(client):
    seen, _ = await _reroll_with_overrides(client, {"prompt": "original"}, {"params": "edited"})
    assert seen == {"prompt": "original"}
