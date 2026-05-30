from __future__ import annotations

from backend.database import (
    add_message,
    get_messages_before,
    insert_workflow_attachment_row,
    set_active_leaf,
)


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "history"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def test_get_messages_before_returns_empty_for_root_message(client):
    cid = await _new_conversation(client)
    root_mid, _ = await add_message(cid, "user", "u0", 0)
    await set_active_leaf(cid, root_mid)
    assert await get_messages_before(cid, root_mid) == []


async def test_get_messages_before_returns_empty_for_missing_message(client):
    cid = await _new_conversation(client)
    assert await get_messages_before(cid, 99999) == []


async def test_get_messages_before_excludes_anchor(client):
    cid = await _new_conversation(client)
    m1, _ = await add_message(cid, "user", "u1", 0)
    m2, _ = await add_message(cid, "assistant", "a2", 0, parent_id=m1)
    m3, _ = await add_message(cid, "user", "u3", 1, parent_id=m2)
    await set_active_leaf(cid, m3)
    msgs = await get_messages_before(cid, m3)
    ids = [m["id"] for m in msgs]
    assert ids == [m1, m2]


async def test_get_messages_before_returns_root_to_leaf_order(client):
    cid = await _new_conversation(client)
    m1, _ = await add_message(cid, "user", "u1", 0)
    m2, _ = await add_message(cid, "assistant", "a2", 0, parent_id=m1)
    m3, _ = await add_message(cid, "user", "u3", 1, parent_id=m2)
    m4, _ = await add_message(cid, "assistant", "a4", 1, parent_id=m3)
    await set_active_leaf(cid, m4)
    msgs = await get_messages_before(cid, m4)
    assert [m["id"] for m in msgs] == [m1, m2, m3]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]


async def test_get_messages_before_populates_split_attachment_fields(client):
    cid = await _new_conversation(client)
    m1, _ = await add_message(
        cid,
        "user",
        "look",
        0,
        attachments=[{"mime_type": "image/png", "data_b64": "WA==", "filename": "p"}],
    )
    m2, _ = await add_message(cid, "assistant", "ok", 0, parent_id=m1)
    await set_active_leaf(cid, m2)
    await insert_workflow_attachment_row(
        m1,
        {"filename": "wf", "mime": "image/png", "data": b"WF", "workflow_id": "wf"},
    )
    msgs = await get_messages_before(cid, m2)
    assert len(msgs) == 1
    m1_row = msgs[0]
    assert len(m1_row["user_attachments"]) == 1
    assert len(m1_row["workflow_attachments"]) == 1
    assert "attachments" not in m1_row, "no legacy compat field"
