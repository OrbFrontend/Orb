"""Integration tests for Document mode: CRUD, span roundtrip, 404s, the
spans-without-content guard, and the generate proxy in both transports.

The chat fallback calls ``complete()`` with no tools/tool_choice, which
``_pass_from_tool_choice`` routes to the **writer** queue — so chat-mode doc
tests use ``enqueue_writer``; only text-mode tests use ``enqueue_raw``.
"""

from __future__ import annotations


def _parse_sse(text: str) -> list[dict]:
    """Parse an SSE body into ``[{event, data}]``, unescaping ``\\n`` and
    dropping ``: keepalive`` comment frames (mirrors the wire contract)."""
    events = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev: dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = line[6:].replace("\\n", "\n")
        events.append(ev)
    return events


async def _activate_text_endpoint(client) -> None:
    ep = (await client.post("/api/endpoints", json={"url": "http://llama.local", "api_key": ""})).json()
    await client.put(f"/api/endpoints/{ep['id']}", json={"completion_mode": "text"})
    await client.put("/api/settings", json={"active_endpoint_id": ep["id"]})


async def test_document_crud_lifecycle(client):
    created = (await client.post("/api/documents", json={})).json()
    did = created["id"]
    assert created["title"] == "Untitled"

    # list projection carries no content
    listed = (await client.get("/api/documents")).json()
    assert any(d["id"] == did for d in listed)
    assert "content" not in listed[0]

    # update content + title + spans together
    r = await client.put(
        "/api/documents/" + did,
        json={"title": "My Doc", "content": "hello world", "generated_spans": [{"start": 6, "end": 11}]},
    )
    assert r.status_code == 200

    got = (await client.get("/api/documents/" + did)).json()
    assert got["title"] == "My Doc"
    assert got["content"] == "hello world"
    assert got["generated_spans"] == [{"start": 6, "end": 11}]

    assert (await client.delete("/api/documents/" + did)).status_code == 200
    assert (await client.get("/api/documents/" + did)).status_code == 404


async def test_span_json_roundtrip_multiple(client):
    did = (await client.post("/api/documents", json={"title": "Spans"})).json()["id"]
    spans = [{"start": 0, "end": 3}, {"start": 10, "end": 25}]
    await client.put("/api/documents/" + did, json={"content": "abc...", "generated_spans": spans})
    got = (await client.get("/api/documents/" + did)).json()
    assert got["generated_spans"] == spans


async def test_404s(client):
    assert (await client.get("/api/documents/nope")).status_code == 404
    assert (await client.put("/api/documents/nope", json={"title": "x"})).status_code == 404
    assert (await client.delete("/api/documents/nope")).status_code == 404
    # generate 404s an unknown id before minting any lock/abort entry
    r = await client.post("/api/documents/nope/generate", json={"prompt": "hi"})
    assert r.status_code == 404


async def test_update_spans_without_content_is_422(client):
    did = (await client.post("/api/documents", json={})).json()["id"]
    r = await client.put("/api/documents/" + did, json={"generated_spans": [{"start": 0, "end": 1}]})
    assert r.status_code == 422
    # title-only is fine
    assert (await client.put("/api/documents/" + did, json={"title": "ok"})).status_code == 200


async def test_generate_chat_mode(client, llm_mock):
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_writer(" and then the sky opened.")

    r = await client.post("/api/documents/" + did + "/generate", json={"prompt": "Once upon a time"})
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert events[0] == {"event": "token", "data": " and then the sky opened."}
    assert events[-1]["event"] == "done"

    # chat fallback shape: exactly [system, user=prompt]
    msgs = llm_mock.captured[-1]["messages"]
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[1]["content"] == "Once upon a time"
    assert not llm_mock.raw_calls


async def test_generate_text_mode(client, llm_mock):
    await _activate_text_endpoint(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_raw(" a raw continuation")

    r = await client.post("/api/documents/" + did + "/generate", json={"prompt": "verbatim prefix"})
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert events[0] == {"event": "token", "data": " a raw continuation"}
    assert events[-1]["event"] == "done"

    # complete_raw got the prompt verbatim; no chat fallback fired.
    assert llm_mock.raw_calls[-1]["prompt"] == "verbatim prefix"


async def test_generate_preserves_newlines(client, llm_mock):
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_writer("line one\nline two")
    r = await client.post("/api/documents/" + did + "/generate", json={"prompt": "x"})
    events = _parse_sse(r.text)
    assert events[0]["data"] == "line one\nline two"


async def test_stop_with_and_without_active_token(client):
    did = (await client.post("/api/documents", json={})).json()["id"]
    # no active generation → still a clean 200
    assert (await client.post("/api/documents/" + did + "/stop")).json() == {"ok": True}
