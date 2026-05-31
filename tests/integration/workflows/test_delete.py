from __future__ import annotations

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.database.queries.workflow_attachments import get_workflow_attachment_by_id

from ._fixtures import must_get_workflow_attachment


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "delete"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_message(client) -> tuple[str, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    return cid, mid


async def _insert(mid: int, *, parent: int | None = None, annotation: str | None = None, evicted: bool = False) -> int:
    att: dict = {"filename": "a", "mime": "image/png", "data": b"A", "workflow_id": "img"}
    if parent is not None:
        att["parent_attachment_id"] = parent
    if annotation is not None:
        att["annotation"] = annotation
    return await insert_workflow_attachment_row(mid, att, insert_as_evicted=evicted)


async def _delete(client, cid: str, mid: int, aid: int, scope):
    return await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete",
        json={"scope": scope},
    )


async def _activate(client, cid: str, mid: int, root_id: int, sibling_id: int | None) -> None:
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": sibling_id},
    )
    assert resp.status_code == 200


async def test_variant_delete_of_active_nulls_root_pointer(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid)
    sib = await _insert(mid, parent=root)
    await _activate(client, cid, mid, root, sib)
    resp = await _delete(client, cid, mid, sib, "variant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [sib]
    assert body["group_empty"] is False
    assert body["root_id"] == root
    assert body["active_sibling_id"] is None
    assert await get_workflow_attachment_by_id(sib) is None
    row = await must_get_workflow_attachment(root)
    assert row["active_sibling_id"] is None


async def test_variant_delete_of_non_active_keeps_root_pointer(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid)
    sib1 = await _insert(mid, parent=root)
    sib2 = await _insert(mid, parent=root)
    await _activate(client, cid, mid, root, sib2)
    resp = await _delete(client, cid, mid, sib1, "variant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [sib1]
    assert body["active_sibling_id"] == sib2
    assert await get_workflow_attachment_by_id(sib1) is None
    row = await must_get_workflow_attachment(root)
    assert row["active_sibling_id"] == sib2


async def test_variant_delete_of_root_promotes_survivor_and_carries_annotation(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid, annotation="ROOT")
    sib1 = await _insert(mid, parent=root, annotation="SIB1")
    sib2 = await _insert(mid, parent=root, annotation="SIB2")
    await _activate(client, cid, mid, root, sib2)
    resp = await _delete(client, cid, mid, root, "variant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [root]
    assert body["group_empty"] is False
    assert body["root_id"] == sib1
    assert body["active_sibling_id"] == sib2
    assert await get_workflow_attachment_by_id(root) is None
    new_root = await must_get_workflow_attachment(sib1)
    assert new_root["parent_attachment_id"] is None
    assert new_root["annotation"] == "ROOT"
    assert new_root["active_sibling_id"] == sib2
    other = await must_get_workflow_attachment(sib2)
    assert other["parent_attachment_id"] == sib1


async def test_variant_delete_of_active_root_resets_active_to_null(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid)
    sib1 = await _insert(mid, parent=root)
    await _insert(mid, parent=root)
    await _activate(client, cid, mid, root, root)
    resp = await _delete(client, cid, mid, root, "variant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["root_id"] == sib1
    assert body["active_sibling_id"] is None
    new_root = await must_get_workflow_attachment(sib1)
    assert new_root["active_sibling_id"] is None


async def test_variant_delete_of_singleton_root_empties_group(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid)
    resp = await _delete(client, cid, mid, root, "variant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [root]
    assert body["group_empty"] is True
    assert await get_workflow_attachment_by_id(root) is None


async def test_group_delete_removes_root_and_all_siblings(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid)
    sib1 = await _insert(mid, parent=root)
    sib2 = await _insert(mid, parent=root, evicted=True)
    resp = await _delete(client, cid, mid, sib1, "group")
    assert resp.status_code == 200
    body = resp.json()
    assert body["group_empty"] is True
    assert sorted(body["deleted_ids"]) == sorted([root, sib1, sib2])
    for aid in (root, sib1, sib2):
        assert await get_workflow_attachment_by_id(aid) is None


async def test_unknown_conversation_returns_404(client):
    resp = await client.post(
        "/api/conversations/no-such/messages/1/workflow-attachments/1/delete",
        json={"scope": "group"},
    )
    assert resp.status_code == 404


async def test_anchor_on_other_conversation_returns_404(client):
    _, mid = await _seed_message(client)
    root = await _insert(mid)
    other_cid = await _new_conversation(client)
    resp = await _delete(client, other_cid, mid, root, "group")
    assert resp.status_code == 404


async def test_attachment_on_other_message_returns_404(client):
    cid, mid = await _seed_message(client)
    other_mid, _ = await add_message(cid, "assistant", "other", 1, parent_id=mid)
    root_other = await _insert(other_mid)
    resp = await _delete(client, cid, mid, root_other, "variant")
    assert resp.status_code == 404


async def test_bad_scope_returns_400(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid)
    resp = await _delete(client, cid, mid, root, "nonsense")
    assert resp.status_code == 400


async def test_missing_scope_returns_400(client):
    cid, mid = await _seed_message(client)
    root = await _insert(mid)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root}/delete",
        json={},
    )
    assert resp.status_code == 400
