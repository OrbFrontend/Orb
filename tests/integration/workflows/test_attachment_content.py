"""The message listing carries attachments without bytes; content routes serve them.

A chat's attachments are megabytes of base64, and inlining them made the one
response that opens a chat the slow part of opening it. The listing now says
only whether a workflow row's bytes are evicted, and each attachment's bytes
load from a content route the browser caches and revalidates.
"""

from __future__ import annotations

from backend.database import (
    add_message,
    insert_workflow_attachment_row,
    set_active_leaf,
)
from backend.workflows.attachment_cache import evict, rehydrate_attachment


async def _chat_with_artifact(client, data: bytes = b"RIFF-audio-bytes", mime: str = "audio/wav") -> tuple[str, int, int]:
    resp = await client.post("/api/conversations", json={"title": "content"})
    assert resp.status_code == 200
    cid = resp.json()["id"]
    mid, _ = await add_message(cid, "assistant", "spoken reply", 0)
    await set_active_leaf(cid, mid)
    aid = await insert_workflow_attachment_row(
        mid,
        {
            "filename": "speech.wav",
            "mime": mime,
            "data": data,
            "workflow_id": "tts",
            "seed": "SEED",
            "generation_metadata": {"voice": "v"},
        },
    )
    return cid, mid, aid


async def _listed_attachment(client, cid: str) -> dict:
    msgs = (await client.get(f"/api/conversations/{cid}/messages")).json()
    return msgs[0]["workflow_attachments"][0]


async def test_listing_carries_metadata_and_eviction_but_no_bytes(client):
    cid, mid, aid = await _chat_with_artifact(client)

    att = await _listed_attachment(client, cid)
    assert "data_b64" not in att
    assert att["id"] == aid
    assert att["message_id"] == mid
    assert att["mime_type"] == "audio/wav"
    assert att["seed"] == "SEED"
    assert att["evicted"] == 0

    await evict(aid)
    assert (await _listed_attachment(client, cid))["evicted"] == 1


async def test_listing_carries_user_uploads_without_bytes(client):
    resp = await client.post("/api/conversations", json={"title": "upload"})
    cid = resp.json()["id"]
    mid, _ = await add_message(
        cid,
        "user",
        "look",
        0,
        attachments=[{"mime_type": "image/png", "data_b64": "QUJD", "filename": "a.png", "size": 3}],
    )
    await set_active_leaf(cid, mid)

    upload = (await client.get(f"/api/conversations/{cid}/messages")).json()[0]["user_attachments"][0]
    assert "data_b64" not in upload
    assert upload["size"] == 3

    content = await client.get(f"/api/user-attachments/{upload['id']}/content")
    assert content.status_code == 200
    assert content.content == b"ABC"
    assert content.headers["content-type"] == "image/png"


async def test_content_route_serves_bytes_and_revalidates(client):
    _, _, aid = await _chat_with_artifact(client, data=b"RIFF-audio-bytes")

    resp = await client.get(f"/api/workflow-attachments/{aid}/content")
    assert resp.status_code == 200
    assert resp.content == b"RIFF-audio-bytes"
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.headers["cache-control"] == "private, no-cache"
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-security-policy"].startswith("sandbox")

    etag = resp.headers["etag"]
    again = await client.get(f"/api/workflow-attachments/{aid}/content", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


async def test_rehydrated_bytes_do_not_revalidate_as_the_old_ones(client):
    # Why the route revalidates instead of letting the browser keep a copy: an
    # evicted row is refilled under the same id, possibly with different bytes.
    _, _, aid = await _chat_with_artifact(client, data=b"FIRST-TAKE")
    old_etag = (await client.get(f"/api/workflow-attachments/{aid}/content")).headers["etag"]

    await evict(aid)
    assert (await client.get(f"/api/workflow-attachments/{aid}/content")).status_code == 410

    await rehydrate_attachment(aid, b"REGENERATED")
    resp = await client.get(f"/api/workflow-attachments/{aid}/content", headers={"If-None-Match": old_etag})
    assert resp.status_code == 200
    assert resp.content == b"REGENERATED"


async def test_missing_attachment_is_404(client):
    assert (await client.get("/api/workflow-attachments/999999/content")).status_code == 404
    assert (await client.get("/api/user-attachments/999999/content")).status_code == 404


async def test_byte_ranges(client):
    _, _, aid = await _chat_with_artifact(client, data=b"0123456789")
    url = f"/api/workflow-attachments/{aid}/content"

    async def get(range_header: str):
        return await client.get(url, headers={"Range": range_header})

    span = await get("bytes=2-5")
    assert span.status_code == 206
    assert span.content == b"2345"
    assert span.headers["content-range"] == "bytes 2-5/10"

    assert (await get("bytes=7-")).content == b"789"
    assert (await get("bytes=-3")).content == b"789"
    assert (await get("bytes=8-99")).headers["content-range"] == "bytes 8-9/10"

    unsatisfiable = await get("bytes=10-")
    assert unsatisfiable.status_code == 416
    assert unsatisfiable.headers["content-range"] == "bytes */10"

    # Ranges this does not understand are ignored, not refused.
    for ignored in ("bytes=5-2", "bytes=0-1,4-5", "items=0-1"):
        whole = await get(ignored)
        assert whole.status_code == 200, ignored
        assert whole.content == b"0123456789"

    stale = await client.get(url, headers={"Range": "bytes=0-1", "If-Range": '"stale"'})
    assert stale.status_code == 200


async def test_ill_formed_mime_is_served_as_opaque_bytes(client):
    _, _, aid = await _chat_with_artifact(client, data=b"<script>x</script>", mime="text/html; charset=utf-8")
    resp = await client.get(f"/api/workflow-attachments/{aid}/content")
    assert resp.headers["content-type"] == "application/octet-stream"


async def test_undecodable_upload_bytes_are_422(client, db):
    resp = await client.post("/api/conversations", json={"title": "bad"})
    cid = resp.json()["id"]
    mid, _ = await add_message(cid, "user", "x", 0)
    cur = await db.execute(
        "INSERT INTO user_attachments (message_id, mime_type, data_b64, created_at) VALUES (?, ?, ?, ?)",
        (mid, "image/png", "not base64!", "2026-01-01T00:00:00+00:00"),
    )
    await db.commit()
    assert (await client.get(f"/api/user-attachments/{cur.lastrowid}/content")).status_code == 422
