import asyncio

from backend.api.deps import _workflow_root_lock
from backend.database import add_message, insert_workflow_attachment_row, set_active_leaf

from ._fixtures import must_get_workflow_attachment, new_conversation


async def _seed_root_with_sibling(client) -> tuple[str, int, int, int]:
    cid = await new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene", 0)
    await set_active_leaf(cid, mid)
    root_id = await insert_workflow_attachment_row(
        mid, {"filename": "r", "mime": "image/png", "data": b"R", "workflow_id": "img"}
    )
    sib_id = await insert_workflow_attachment_row(
        mid, {"filename": "s", "mime": "image/png", "data": b"S", "workflow_id": "img", "parent_attachment_id": root_id}
    )
    return cid, mid, root_id, sib_id


async def test_root_on_wrong_message_returns_404(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    other_mid, _ = await add_message(cid, "assistant", "other", 1, parent_id=mid)
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{other_mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": sib_id},
        expected_status=404,
    )


async def test_non_root_target_returns_400(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{sib_id}/activate",
        json={"sibling_id": sib_id},
        expected_status=400,
    )


async def test_sibling_id_none_clears_active(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    await client.post(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate", json={"sibling_id": sib_id}
    )
    resp = await client.post_json(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate", json={"sibling_id": None}
    )
    assert resp == {"active_sibling_id": None}
    assert (await must_get_workflow_attachment(root_id))["active_sibling_id"] is None


async def test_sibling_id_int_sets_active(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    resp = await client.post_json(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate", json={"sibling_id": sib_id}
    )
    assert resp == {"active_sibling_id": sib_id}
    assert (await must_get_workflow_attachment(root_id))["active_sibling_id"] == sib_id


async def test_sibling_id_non_int_non_null_returns_400(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": "not-an-int"},
        expected_status=400,
    )


async def test_sibling_id_bool_rejected(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": True},
        expected_status=400,
    )


async def test_sibling_id_missing_target_returns_404(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": 99999},
        expected_status=404,
    )


async def test_sibling_id_from_different_group_returns_400(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    # Independent root on the same message.
    other_root = await insert_workflow_attachment_row(
        mid, {"filename": "o", "mime": "image/png", "data": b"O", "workflow_id": "img"}
    )
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate",
        json={"sibling_id": other_root},
        expected_status=400,
    )


async def test_activate_not_blocked_by_held_root_lock(client):
    """A swipe must land while a reroll/regen holds the group's root lock.

    Those routes hold it for the whole render (a minute+ for image gen). If /activate waited on it, artifact navigation would
    freeze for the duration: one click hangs, and the frontend's per-root in-flight guard then drops every later click.
    """
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    async with _workflow_root_lock(root_id):
        resp = await asyncio.wait_for(
            client.post(
                f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate", json={"sibling_id": sib_id}
            ),
            timeout=5,
        )
    assert resp.status_code == 200
    assert (await must_get_workflow_attachment(root_id))["active_sibling_id"] == sib_id


async def test_sibling_id_equal_to_root_accepted(client):
    cid, mid, root_id, sib_id = await _seed_root_with_sibling(client)
    await client.post_checked(
        f"/api/conversations/{cid}/messages/{mid}/workflow-attachments/{root_id}/activate", json={"sibling_id": root_id}
    )
    assert (await must_get_workflow_attachment(root_id))["active_sibling_id"] == root_id
