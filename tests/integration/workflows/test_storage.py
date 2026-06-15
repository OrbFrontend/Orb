from __future__ import annotations

import base64
import json
import os
import tempfile

import pytest

from backend.database import (
    add_message,
    get_messages,
    get_user_attachments_for_message,
    get_workflow_attachment_by_id,
    get_workflow_attachments_for_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)

from ._fixtures import make_workflow, must_get_workflow_attachment, register_for_test


@pytest.fixture(autouse=True)
def _register_artifact_workflows():
    """Register the workflow ids the tests in this module name in
    attachment dicts. ``add_message`` routes workflow-source attachments
    through the cache batch helper, which gates on
    ``produces_artifacts=True``; without registration the gate would drop
    the attachment before it ever reached the ``workflow_attachments``
    table the tests inspect."""
    wf = make_workflow(
        "wf",
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    imagebot = make_workflow(
        "imagebot",
        produces_artifacts=True,
        regenerate=lambda ctx, body: [],
        reroll_gen=lambda ctx, params, seed: b"",
    )
    with register_for_test(wf), register_for_test(imagebot):
        yield


async def _new_conversation(client) -> str:
    resp = await client.post("/api/conversations", json={"title": "Storage test"})
    assert resp.status_code == 200
    return resp.json()["id"]


async def _seed_message(client) -> tuple[str, int]:
    cid = await _new_conversation(client)
    mid, _ = await add_message(cid, "assistant", "scene draft", 0)
    await set_active_leaf(cid, mid)
    return cid, mid


async def test_insert_workflow_attachment_row_happy_path_persists_all_fields(client):
    cid, mid = await _seed_message(client)
    att_id = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "render.png",
            "mime": "image/png",
            "data": b"PNG_BYTES",
            "workflow_id": "imagebot",
            "parent_attachment_id": None,
            "annotation": "[image: 1 frame]",
            "seed": "deadbeef",
            "generation_metadata": {"steps": 4, "guidance": 7.5},
        },
    )
    row = await must_get_workflow_attachment(att_id)
    assert row["message_id"] == mid
    assert row["mime_type"] == "image/png"
    assert row["filename"] == "render.png"
    assert row["data_b64"] == base64.b64encode(b"PNG_BYTES").decode("ascii")
    assert row["workflow_id"] == "imagebot"
    assert row["parent_attachment_id"] is None
    assert row["annotation"] == "[image: 1 frame]"
    assert row["seed"] == "deadbeef"
    assert json.loads(row["generation_metadata"]) == {"steps": 4, "guidance": 7.5}


async def test_insert_workflow_attachment_row_rejects_missing_workflow_id(client):
    cid, mid = await _seed_message(client)
    with pytest.raises(ValueError, match="workflow_id"):
        await insert_workflow_attachment_row(
            mid,
            {"filename": "x.png", "mime": "image/png", "data": b"X", "workflow_id": ""},
        )


async def test_insert_workflow_attachment_row_rejects_both_data_and_path(client):
    cid, mid = await _seed_message(client)
    with pytest.raises(ValueError, match="exactly one"):
        await insert_workflow_attachment_row(
            mid,
            {
                "filename": "x.png",
                "mime": "image/png",
                "data": b"X",
                "path": "/tmp/x",
                "workflow_id": "wf",
            },
        )


async def test_insert_workflow_attachment_row_rejects_neither_data_nor_path(client):
    cid, mid = await _seed_message(client)
    with pytest.raises(ValueError, match="exactly one"):
        await insert_workflow_attachment_row(
            mid,
            {"filename": "x.png", "mime": "image/png", "workflow_id": "wf"},
        )


async def test_insert_workflow_attachment_row_rejects_non_bytes_data(client):
    cid, mid = await _seed_message(client)
    with pytest.raises(ValueError, match="data must be bytes"):
        await insert_workflow_attachment_row(
            mid,
            {"filename": "x.png", "mime": "image/png", "data": "not-bytes", "workflow_id": "wf"},
        )


async def test_insert_workflow_attachment_row_rejects_empty_bytes(client):
    cid, mid = await _seed_message(client)
    with pytest.raises(ValueError, match="empty"):
        await insert_workflow_attachment_row(
            mid,
            {"filename": "x.png", "mime": "image/png", "data": b"", "workflow_id": "wf"},
        )


async def test_insert_workflow_attachment_row_rejects_empty_path_file(client):
    cid, mid = await _seed_message(client)
    with tempfile.NamedTemporaryFile(delete=False) as f:
        empty_path = f.name
    try:
        with pytest.raises(ValueError, match="empty"):
            await insert_workflow_attachment_row(
                mid,
                {"filename": "x", "mime": "application/octet-stream", "path": empty_path, "workflow_id": "wf"},
            )
    finally:
        os.unlink(empty_path)


async def test_insert_workflow_attachment_row_rejects_missing_message(client):  # noqa: ARG001
    with pytest.raises(LookupError, match="does not exist"):
        await insert_workflow_attachment_row(
            999999,
            {"filename": "x.png", "mime": "image/png", "data": b"X", "workflow_id": "wf"},
        )


async def test_insert_workflow_attachment_row_rejects_path_outside_staging_root(client):
    """A path-shape attachment pointing outside the staging root is refused
    before any open()/stat(), so a workflow cannot disclose arbitrary files."""
    cid, mid = await _seed_message(client)
    with pytest.raises(ValueError, match="staging root"):
        await insert_workflow_attachment_row(
            mid,
            {"filename": "passwd", "mime": "text/plain", "path": "/etc/passwd", "workflow_id": "wf"},
        )


async def test_insert_workflow_attachment_row_rejects_traversal_escape(client):
    """``..`` segments that resolve outside the staging root are refused."""
    cid, mid = await _seed_message(client)
    escape = os.path.join(tempfile.gettempdir(), "..", "etc", "passwd")
    with pytest.raises(ValueError, match="staging root"):
        await insert_workflow_attachment_row(
            mid,
            {"filename": "passwd", "mime": "text/plain", "path": escape, "workflow_id": "wf"},
        )


async def test_insert_workflow_attachment_row_path_shape_reads_bytes(client):
    cid, mid = await _seed_message(client)
    payload = b"DATA_FROM_PATH"
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(payload)
        path = f.name
    try:
        att_id = await insert_workflow_attachment_row(
            mid,
            {"filename": "x", "mime": "application/octet-stream", "path": path, "workflow_id": "wf"},
        )
    finally:
        os.unlink(path)
    row = await must_get_workflow_attachment(att_id)
    assert row["data_b64"] == base64.b64encode(payload).decode("ascii")


async def test_get_workflow_attachment_by_id_returns_none_when_absent(client):  # noqa: ARG001
    assert await get_workflow_attachment_by_id(999999) is None


async def test_get_workflow_attachment_by_id_returns_full_column_set(client):
    cid, mid = await _seed_message(client)
    att_id = await insert_workflow_attachment_row(
        mid,
        {"filename": "x.png", "mime": "image/png", "data": b"X", "workflow_id": "wf"},
    )
    row = await must_get_workflow_attachment(att_id)
    expected_keys = {
        "id",
        "message_id",
        "mime_type",
        "data_b64",
        "filename",
        "created_at",
        "workflow_id",
        "parent_attachment_id",
        "annotation",
        "seed",
        "generation_metadata",
        "consumption_metadata",
        "active_sibling_id",
        "recent_accesses",
    }
    assert set(row.keys()) == expected_keys
    assert row["consumption_metadata"] is None


async def test_split_attachment_fields_populated_independently(client):
    cid, mid = await _seed_message(client)
    user_mid, _ = await add_message(
        cid,
        "user",
        "look at this",
        0,
        parent_id=mid,
        attachments=[{"mime_type": "image/png", "data_b64": "VVA==", "filename": "up.png", "size": 4}],
    )
    await set_active_leaf(cid, user_mid)
    await insert_workflow_attachment_row(
        mid,
        {"filename": "wf.bin", "mime": "application/octet-stream", "data": b"WF", "workflow_id": "wf"},
    )
    msgs = await get_messages(cid)
    by_id = {m["id"]: m for m in msgs}
    assert by_id[user_mid]["user_attachments"] and len(by_id[user_mid]["user_attachments"]) == 1
    assert by_id[user_mid]["user_attachments"][0]["filename"] == "up.png"
    assert by_id[user_mid]["workflow_attachments"] == []
    assert by_id[mid]["user_attachments"] == []
    assert by_id[mid]["workflow_attachments"] and len(by_id[mid]["workflow_attachments"]) == 1
    assert by_id[mid]["workflow_attachments"][0]["workflow_id"] == "wf"


async def test_legacy_attachments_field_absent(client):
    """Reading messages must not synthesize a legacy ``attachments`` field; readers must consume ``user_attachments`` and ``workflow_attachments`` separately."""
    cid, mid = await _seed_message(client)
    await insert_workflow_attachment_row(
        mid,
        {"filename": "x", "mime": "image/png", "data": b"X", "workflow_id": "wf"},
    )
    msgs = await get_messages(cid)
    for m in msgs:
        assert "attachments" not in m


async def test_get_user_attachments_for_message_ignores_workflow_rows(client):
    cid, mid = await _seed_message(client)
    await insert_workflow_attachment_row(
        mid,
        {"filename": "wf.bin", "mime": "image/png", "data": b"X", "workflow_id": "wf"},
    )
    rows = await get_user_attachments_for_message(mid)
    assert rows == []


async def test_get_workflow_attachments_for_message_returns_full_columns(client):
    cid, mid = await _seed_message(client)
    await insert_workflow_attachment_row(
        mid,
        {
            "filename": "wf.bin",
            "mime": "image/png",
            "data": b"X",
            "workflow_id": "wf",
            "seed": "abc",
            "generation_metadata": {"k": "v"},
        },
    )
    rows = await get_workflow_attachments_for_message(mid)
    assert len(rows) == 1
    row = rows[0]
    assert row["seed"] == "abc"
    assert json.loads(row["generation_metadata"]) == {"k": "v"}
    assert "data_b64" in row


async def test_add_message_workflow_attachment_lands_in_workflow_table(client):
    cid = await _new_conversation(client)
    user_mid, _ = await add_message(cid, "user", "hi", 0)
    await set_active_leaf(cid, user_mid)
    asst_mid, _ = await add_message(
        cid,
        "assistant",
        "hello",
        0,
        parent_id=user_mid,
        attachments=[
            {
                "source": "workflow:wf",
                "workflow_id": "wf",
                "filename": "x.png",
                "mime": "image/png",
                "data": b"XYZ",
                "annotation": "[image]",
            }
        ],
    )
    user_rows = await get_user_attachments_for_message(asst_mid)
    workflow_rows = await get_workflow_attachments_for_message(asst_mid)
    assert user_rows == []
    assert len(workflow_rows) == 1
    assert workflow_rows[0]["workflow_id"] == "wf"
    assert workflow_rows[0]["annotation"] == "[image]"


async def test_add_message_user_upload_lands_in_user_table(client):
    cid = await _new_conversation(client)
    mid, _ = await add_message(
        cid,
        "user",
        "hello",
        0,
        attachments=[{"mime_type": "image/png", "data_b64": "WA==", "filename": "p.png", "size": 1}],
    )
    user_rows = await get_user_attachments_for_message(mid)
    workflow_rows = await get_workflow_attachments_for_message(mid)
    assert len(user_rows) == 1 and user_rows[0]["filename"] == "p.png"
    assert workflow_rows == []
