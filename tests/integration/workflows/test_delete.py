from backend.database import add_message, insert_workflow_attachment_row
from backend.database.queries.workflow_attachments import get_workflow_attachment_by_id

from ._fixtures import must_get_workflow_attachment, new_conversation, seed_message


async def _insert(mid: int, *, parent: int | None = None, annotation: str | None = None, evicted: bool = False) -> int:
    att: dict = {"filename": "a", "mime": "image/png", "data": b"A", "workflow_id": "img"}
    if parent is not None:
        att["parent_attachment_id"] = parent
    if annotation is not None:
        att["annotation"] = annotation
    return await insert_workflow_attachment_row(mid, att, insert_as_evicted=evicted)


async def _delete(client, cid: str, mid: int, aid: int, scope):
    return await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete", json={"scope": scope}
    )


async def _activate(client, cid: str, mid: int, root_id: int, sibling_id: int | None) -> None:
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate", json={"sibling_id": sibling_id}
    )


async def test_variant_delete_of_active_nulls_root_pointer(client):
    cid, mid = await seed_message(client)
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
    assert (await must_get_workflow_attachment(root))["active_sibling_id"] is None


async def test_variant_delete_of_non_active_keeps_root_pointer(client):
    cid, mid = await seed_message(client)
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
    assert (await must_get_workflow_attachment(root))["active_sibling_id"] == sib2


async def test_variant_delete_of_root_promotes_survivor_and_carries_annotation(client):
    cid, mid = await seed_message(client)
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
    assert (await must_get_workflow_attachment(sib2))["parent_attachment_id"] == sib1


async def test_variant_delete_of_active_root_resets_active_to_null(client):
    cid, mid = await seed_message(client)
    root = await _insert(mid)
    sib1 = await _insert(mid, parent=root)
    await _insert(mid, parent=root)
    await _activate(client, cid, mid, root, root)
    resp = await _delete(client, cid, mid, root, "variant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["root_id"] == sib1
    assert body["active_sibling_id"] is None
    assert (await must_get_workflow_attachment(sib1))["active_sibling_id"] is None


async def test_variant_delete_of_singleton_root_empties_group(client):
    cid, mid = await seed_message(client)
    root = await _insert(mid)
    resp = await _delete(client, cid, mid, root, "variant")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [root]
    assert body["group_empty"] is True
    assert await get_workflow_attachment_by_id(root) is None


async def test_group_delete_removes_root_and_all_siblings(client):
    cid, mid = await seed_message(client)
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


async def test_group_delete_spares_variant_landed_after_confirm(client):
    cid, mid = await seed_message(client)
    root = await _insert(mid, annotation="ROOT")
    late = await _insert(mid, parent=root)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root}/delete",
        json={"scope": "group", "seen": [root]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [root]
    assert body["group_empty"] is False
    assert body["root_id"] == late
    assert await get_workflow_attachment_by_id(root) is None
    kept = await must_get_workflow_attachment(late)
    assert kept["parent_attachment_id"] is None
    assert kept["annotation"] == "ROOT"


async def test_others_delete_of_sibling_promotes_it_and_carries_annotation(client):
    cid, mid = await seed_message(client)
    root = await _insert(mid, annotation="ROOT")
    sib1 = await _insert(mid, parent=root, annotation="SIB1")
    sib2 = await _insert(mid, parent=root, annotation="SIB2")
    await _activate(client, cid, mid, root, sib1)
    resp = await _delete(client, cid, mid, sib1, "others")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [root, sib2]
    assert body["group_empty"] is False
    assert body["root_id"] == sib1
    assert body["active_sibling_id"] is None
    for aid in (root, sib2):
        assert await get_workflow_attachment_by_id(aid) is None
    kept = await must_get_workflow_attachment(sib1)
    assert kept["parent_attachment_id"] is None
    assert kept["annotation"] == "ROOT"
    assert kept["active_sibling_id"] is None


async def test_others_delete_of_root_removes_every_sibling(client):
    cid, mid = await seed_message(client)
    root = await _insert(mid, annotation="ROOT")
    sib1 = await _insert(mid, parent=root)
    sib2 = await _insert(mid, parent=root)
    await _activate(client, cid, mid, root, sib2)
    resp = await _delete(client, cid, mid, root, "others")
    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted_ids"] == [sib1, sib2]
    assert body["root_id"] == root
    kept = await must_get_workflow_attachment(root)
    assert kept["annotation"] == "ROOT"
    assert kept["active_sibling_id"] is None


async def test_anchor_on_other_conversation_returns_404(client):
    _, mid = await seed_message(client)
    root = await _insert(mid)
    assert (await _delete(client, await new_conversation(client), mid, root, "group")).status_code == 404


async def test_attachment_on_other_message_returns_404(client):
    cid, mid = await seed_message(client)
    other_mid, _ = await add_message(cid, "assistant", "other", 1, parent_id=mid)
    assert (await _delete(client, cid, mid, await _insert(other_mid), "variant")).status_code == 404


async def test_bad_scope_returns_400(client):
    cid, mid = await seed_message(client)
    assert (await _delete(client, cid, mid, await _insert(mid), "nonsense")).status_code == 400


async def test_missing_scope_returns_400(client):
    cid, mid = await seed_message(client)
    root = await _insert(mid)
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root}/delete", json={}, expected_status=400
    )


async def test_others_delete_keeps_one_root_when_the_old_root_is_unseen(client):
    cid, mid = await seed_message(client)
    root = await _insert(mid, annotation="ROOT")
    kept = await _insert(mid, parent=root)
    resp = await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{kept}/delete",
        json={"scope": "others", "seen": [kept]},
    )
    assert resp.json()["root_id"] == kept
    assert (await must_get_workflow_attachment(kept))["parent_attachment_id"] is None
    assert (await must_get_workflow_attachment(root))["parent_attachment_id"] == kept
