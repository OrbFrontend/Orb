from __future__ import annotations

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)

from ._fixtures import must_get_workflow_attachment


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "activate"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_root_with_sibling(client) -> tuple[str, int, int, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    root_id = await insert_workflow_attachment_row(
        mid,
        {"filename": "r", "mime": "image/png", "data": b"R", "workflow_id": "img"},
    )
    sib_id = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "s",
            "mime": "image/png",
            "data": b"S",
            "workflow_id": "img",
            "parent_attachment_id": root_id,
        },
    )
    return cid, mid, root_id, sib_id


async def test_unknown_conversation_returns_404(client):
    resp = await client.post(
        "/api/conversations/no-such/messages/1/workflow-attachments/1/activate",
        json={"sibling_id": 1},
    )
    assert resp.status_code == 404


async def test_root_attachment_not_found_returns_404(client):
    cid = await _new_conversation(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/1/workflow-attachments/99999/activate",
        json={"sibling_id": 1},
    )
    assert resp.status_code == 404


async def test_root_on_wrong_message_returns_404(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    other_mid, _ = await add_message(cid, "assistant", "other", 1, parent_id=mid)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{other_mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": sib_id},
    )
    assert resp.status_code == 404


async def test_non_root_target_returns_400(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{sib_id}/activate",
        json={"sibling_id": sib_id},
    )
    assert resp.status_code == 400


async def test_sibling_id_none_clears_active(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": sib_id},
    )
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": None},
    )
    assert resp.status_code == 200
    assert resp.json() == {"active_sibling_id": None}
    row = await must_get_workflow_attachment(root_id)
    assert row["active_sibling_id"] is None


async def test_sibling_id_int_sets_active(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": sib_id},
    )
    assert resp.status_code == 200
    assert resp.json() == {"active_sibling_id": sib_id}
    row = await must_get_workflow_attachment(root_id)
    assert row["active_sibling_id"] == sib_id


async def test_sibling_id_non_int_non_null_returns_400(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": "not-an-int"},
    )
    assert resp.status_code == 400


async def test_sibling_id_bool_rejected(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": True},
    )
    assert resp.status_code == 400


async def test_sibling_id_missing_target_returns_404(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": 99999},
    )
    assert resp.status_code == 404


async def test_sibling_id_from_different_group_returns_400(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    # Independent root on the same message.
    other_root = await insert_workflow_attachment_row(
        mid,
        {"filename": "o", "mime": "image/png", "data": b"O", "workflow_id": "img"},
    )
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": other_root},
    )
    assert resp.status_code == 400


async def test_sibling_id_equal_to_root_accepted(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": root_id},
    )
    assert resp.status_code == 200
    row = await must_get_workflow_attachment(root_id)
    assert row["active_sibling_id"] == root_id
