"""Transport-level response shaping: static revalidation and selective gzip.

The page loads ~90 ES modules and ~25 stylesheets unbundled, so whether each
one re-downloads, revalidates, or compresses decides most of a load's bytes.
"""

import uuid

from starlette.datastructures import Headers

from backend.api.compression import compressible


async def test_static_files_revalidate_instead_of_redownloading(client):
    resp = await client.get_checked("/static/app.js")
    assert resp.headers["cache-control"] == "no-cache"
    etag = resp.headers["etag"]

    assert (await client.get_checked("/static/app.js", headers={"If-None-Match": etag}, expected_status=304)).content == b""


async def test_api_responses_stay_uncached(client):
    assert (await client.get("/api/settings")).headers["cache-control"] == "no-store"


async def test_text_responses_are_gzipped(client):
    script = await client.get("/static/app.js", headers={"Accept-Encoding": "gzip"})
    assert script.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in script.headers["vary"].lower()
    assert b"initAll" in script.content  # httpx decodes; the body survives the round trip

    for i in range(40):
        await client.post("/api/conversations", json={"title": f"Chat {i} {uuid.uuid4()}"})
    listing = await client.get("/api/conversations", headers={"Accept-Encoding": "gzip"})
    assert listing.headers["content-encoding"] == "gzip"
    assert len(listing.json()) == 40


async def test_small_responses_skip_compression(client):
    assert "content-encoding" not in (await client.get_checked("/api/themes", headers={"Accept-Encoding": "gzip"})).headers


async def test_media_is_not_recompressed(client):
    resp = await client.get_checked("/static/favicon.png", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in resp.headers


async def test_byte_ranges_are_not_gzipped(client):
    # Content-Range offsets name the uncompressed bytes; a gzipped slice would break media seeking.
    resp = await client.get_checked(
        "/static/app.js", headers={"Accept-Encoding": "gzip", "Range": "bytes=0-4095"}, expected_status=206
    )
    assert "content-encoding" not in resp.headers
    assert len(resp.content) == 4096


def test_compressible_gate():
    def ok(status: int, content_type: str, **extra: str) -> bool:
        return compressible(status, Headers({"content-type": content_type, **extra}))

    assert ok(200, "application/json")
    assert ok(200, "text/javascript; charset=utf-8")
    assert ok(200, "text/css; charset=utf-8")
    assert ok(200, "image/svg+xml")
    assert not ok(200, "text/event-stream")
    assert not ok(200, "image/png")
    assert not ok(200, "audio/mpeg")
    assert not ok(200, "font/woff2")
    assert not ok(206, "text/javascript")
    assert not ok(200, "application/json", **{"content-range": "bytes 0-9/100"})
