"""Integration tests for Document mode: CRUD, span roundtrip, 404s, the
spans-without-content guard, and the generate proxy in both transports.

The chat fallback calls ``complete()`` with no tools/tool_choice, which
``_pass_from_tool_choice`` routes to the **writer** queue — so chat-mode doc
tests use ``enqueue_writer``; only text-mode tests use ``enqueue_raw``.
"""

from __future__ import annotations

import json

from backend.features.documents import (
    DOC_ASSIST_CONTINUE,
    DOC_ASSIST_INSTRUCTION,
    DOC_CHAT_INSTRUCTION,
)
from backend.features.documents.continuation import build_generation_messages


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


async def test_generate_text_mode_assisted_parses_multiturn(client, llm_mock):
    # assisted:true in text mode → parse_doc_macros → complete() (writer queue),
    # NOT complete_raw. The mock sees the parsed multi-turn shape + open prefill.
    await _activate_text_endpoint(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_writer(" a steered continuation")

    prompt = "### USER: be terse\nThe monkey woke. He"
    r = await client.post("/api/documents/" + did + "/generate", json={"prompt": prompt, "assisted": True})
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert events[0] == {"event": "token", "data": " a steered continuation"}
    assert events[-1]["event"] == "done"

    assert not llm_mock.raw_calls  # assisted never touches the raw path
    cap = llm_mock.captured[-1]
    assert cap["messages"] == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": "be terse"},
    ]
    assert cap["params"]["prefill"] == "The monkey woke. He"
    # Reasoning suppressed on every assisted call.
    assert cap["params"]["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False}


async def test_generate_chat_mode_assisted_closes_prefill(client, llm_mock):
    # assisted:true on a chat endpoint → prefill closed as an assistant turn +
    # a re-anchor user turn (chat transport can't hold an open prefill).
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_writer(" continued.")

    prompt = "### USER: keep it dark\nThe hollow"
    r = await client.post("/api/documents/" + did + "/generate", json={"prompt": prompt, "assisted": True})
    assert r.status_code == 200

    cap = llm_mock.captured[-1]
    assert cap["messages"] == [
        {"role": "system", "content": DOC_ASSIST_INSTRUCTION},
        {"role": "user", "content": "keep it dark"},
        {"role": "assistant", "content": "The hollow"},
        {"role": "user", "content": DOC_ASSIST_CONTINUE},
    ]
    assert "prefill" not in cap["params"]


async def test_generate_assisted_defaults_false_hits_raw(client, llm_mock):
    # Omitting `assisted` keeps the verbatim Raw path (complete_raw), unchanged.
    await _activate_text_endpoint(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_raw(" raw")
    await client.post("/api/documents/" + did + "/generate", json={"prompt": "### USER: literal now"})
    # No macro interpretation: the whole prompt went verbatim to complete_raw.
    assert llm_mock.raw_calls[-1]["prompt"] == "### USER: literal now"


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


# ── token_probs wire: `event: probs` frames ───────────────────────────────────

# Mock token records live on a separate channel from the text; keep their token
# strings newline-free so the test-only _parse_sse (which unescapes \n on every
# frame, unlike the real reader) doesn't corrupt the probs JSON.
_PROBS = [{"token": " Paris", "prob": 0.86, "top": [{"t": " Paris", "p": 0.86}, {"t": " Lyon", "p": 0.1}]}]


async def test_generate_text_mode_probs_frames(client, llm_mock):
    await _activate_text_endpoint(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_raw(" Paris", probs=_PROBS)

    r = await client.post(
        "/api/documents/" + did + "/generate",
        json={"prompt": "The capital of France is", "token_probs": True},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)

    # token frame is byte-identical to the no-probs wire.
    assert events[0] == {"event": "token", "data": " Paris"}
    # a probs frame follows, JSON-decoding to the normalized shape.
    probs = [json.loads(e["data"]) for e in events if e["event"] == "probs"]
    assert probs == _PROBS
    assert events[-1]["event"] == "done"
    # n_probs threaded into the /completion request.
    assert llm_mock.raw_calls[-1]["params"]["n_probs"] == 10


async def test_generate_chat_mode_probs_frames(client, llm_mock):
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_writer(" Paris", probs=_PROBS)

    r = await client.post(
        "/api/documents/" + did + "/generate",
        json={"prompt": "The capital of France is", "token_probs": True},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert events[0] == {"event": "token", "data": " Paris"}
    probs = [json.loads(e["data"]) for e in events if e["event"] == "probs"]
    assert probs == _PROBS
    # logprobs threaded into the chat request.
    assert llm_mock.captured[-1]["params"]["logprobs"] is True
    assert llm_mock.captured[-1]["params"]["top_logprobs"] == 5


async def test_generate_no_probs_frames_when_flag_unset(client, llm_mock):
    # Probs enqueued, but token_probs omitted → the continuer sends no n_probs, so
    # the mock (like a real server) returns none; the wire carries only tokens.
    await _activate_text_endpoint(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_raw(" Paris", probs=_PROBS)

    r = await client.post("/api/documents/" + did + "/generate", json={"prompt": "x"})
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert not any(e["event"] == "probs" for e in events)
    assert "n_probs" not in llm_mock.raw_calls[-1]["params"]


async def test_generate_done_event_carries_finish_json(client, llm_mock):
    # The done frame is a JSON dict {"finish": ...} (like the probs channel);
    # the mock's done message carries no finish_reason → empty string.
    did = (await client.post("/api/documents", json={})).json()["id"]
    llm_mock.enqueue_writer("some text")
    r = await client.post("/api/documents/" + did + "/generate", json={"prompt": "x"})
    done = _parse_sse(r.text)[-1]
    assert done["event"] == "done"
    assert json.loads(done["data"]) == {"finish": ""}


# ── Output Auditor: /audit + /patch ───────────────────────────────────────────

_BANNED = "shivers down her spine"


async def _seed_phrase(client, phrase: str = _BANNED) -> None:
    r = await client.post("/api/phrase-bank", json={"variants": [phrase], "kind": "literal"})
    assert r.status_code == 200


async def test_audit_and_patch_404_unknown_document(client):
    body = {"draft": "Some text."}
    assert (await client.post("/api/documents/nope/audit", json=body)).status_code == 404
    assert (await client.post("/api/documents/nope/patch", json=body)).status_code == 404


async def test_audit_clean_draft(client):
    did = (await client.post("/api/documents", json={})).json()["id"]
    r = await client.post(f"/api/documents/{did}/audit", json={"draft": "A perfectly ordinary sentence."})
    assert r.status_code == 200
    body = r.json()
    assert body["report"]["is_clean"] is True
    assert body["report"]["total_issues"] == 0
    assert body["skipped"] is None
    assert body["tail_excluded"] is False


async def test_audit_flags_seeded_phrase(client):
    await _seed_phrase(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    r = await client.post(
        f"/api/documents/{did}/audit",
        json={"draft": f"She felt {_BANNED} again.", "context": "Earlier prose."},
    )
    body = r.json()
    assert body["report"]["total_issues"] >= 1
    hits = body["report"]["sections"]["banned_phrases"]
    assert any(_BANNED in h["phrase"] for h in hits)


async def test_audit_truncated_excludes_tail(client):
    # The banned phrase lives in the dangling half-sentence of a stopped run —
    # trimmed before scanning, so the report is clean and the tail flagged as excluded.
    await _seed_phrase(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    r = await client.post(
        f"/api/documents/{did}/audit",
        json={"draft": f"A clean opening sentence. She felt {_BANNED}", "truncated": True},
    )
    body = r.json()
    assert body["tail_excluded"] is True
    assert body["report"]["is_clean"] is True


async def test_audit_respects_doc_toggles(client):
    await _seed_phrase(client)
    await client.put("/api/settings", json={"document_audit_toggles": {"banned_phrases": False}})
    did = (await client.post("/api/documents", json={})).json()["id"]
    r = await client.post(f"/api/documents/{did}/audit", json={"draft": f"She felt {_BANNED} again."})
    assert r.json()["report"]["is_clean"] is True


async def test_audit_settings_roundtrip(client):
    # New doc-owned columns persist and decode; the chat editor's map is untouched.
    before = (await client.get("/api/settings")).json()
    assert before["document_audit_enabled"] == 1
    assert before["document_audit_autopatch"] == 0
    assert before["document_audit_toggles"]["banned_phrases"] is True

    r = await client.put(
        "/api/settings",
        json={"document_audit_autopatch": True, "document_audit_toggles": {"banned_phrases": False}},
    )
    after = r.json()
    assert after["document_audit_autopatch"] == 1
    assert after["document_audit_toggles"] == {"banned_phrases": False}
    assert after["editor_audit_toggles"] == before["editor_audit_toggles"]


def _enqueue_patch_call(llm_mock, patches: list[dict]) -> None:
    # The /patch route forces editor_apply_patch, which the mock's tool_choice
    # dispatch routes to the editor queue.
    llm_mock.enqueue_editor(
        {
            "tool_calls": [
                {
                    "id": "1",
                    "type": "function",
                    "function": {"name": "editor_apply_patch", "arguments": json.dumps({"patches": patches})},
                }
            ]
        }
    )


async def test_patch_applies_patches(client, llm_mock):
    await _seed_phrase(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    flagged = f"She felt {_BANNED} at once."
    _enqueue_patch_call(llm_mock, [{"search": flagged, "replace": "A chill traced her back."}])

    r = await client.post(f"/api/documents/{did}/patch", json={"draft": flagged, "context": "Earlier prose."})
    assert r.status_code == 200
    body = r.json()
    assert body["patched_draft"] == "A chill traced her back."
    assert body["patch_count"] == 1
    assert body["errors"] == []
    assert body["report_after"]["is_clean"] is True

    # The forced call rode the writer client with the draft as the assistant turn.
    cap = llm_mock.captured[-1]
    assert cap["pass"] == "editor"
    assert {"role": "assistant", "content": flagged} in cap["messages"]
    # KV parity: the patch conversation replays the generation shape verbatim
    # (chat fallback framing + full context as the user turn) and keeps the
    # tool schema out of the rendered prompt.
    assert cap["messages"][0] == {"role": "system", "content": DOC_CHAT_INSTRUCTION}
    assert cap["messages"][1] == {"role": "user", "content": "Earlier prose."}
    assert cap["params"]["tools_in_prompt"] is False


async def test_patch_surfaces_apply_errors(client, llm_mock):
    await _seed_phrase(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    draft = f"She felt {_BANNED} at once."
    _enqueue_patch_call(llm_mock, [{"search": "not present in the draft", "replace": "x"}])

    body = (await client.post(f"/api/documents/{did}/patch", json={"draft": draft})).json()
    assert body["patched_draft"] == draft
    assert body["patch_count"] == 0
    assert len(body["errors"]) == 1
    assert body["report_after"]["total_issues"] >= 1


async def test_patch_clean_draft_skips_llm(client, llm_mock):
    did = (await client.post("/api/documents", json={})).json()["id"]
    body = (await client.post(f"/api/documents/{did}/patch", json={"draft": "Nothing to fix here."})).json()
    assert body["skipped"] == "clean"
    assert body["patched_draft"] == "Nothing to fix here."
    assert llm_mock.calls == [] and llm_mock.raw_calls == []  # no LLM call when clean


async def test_patch_text_mode_raw_extends_prompt_with_json_schema(client, llm_mock):
    await _seed_phrase(client)
    await _activate_text_endpoint(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    flagged = f"She felt {_BANNED} at once."
    ctx = "Earlier prose precedes the run. "
    llm_mock.enqueue_raw(json.dumps({"patches": [{"search": flagged, "replace": "A chill traced her back."}]}))

    body = (await client.post(f"/api/documents/{did}/patch", json={"draft": flagged, "context": ctx})).json()
    assert body["patched_draft"] == "A chill traced her back."
    assert body["patch_count"] == 1

    # Raw transport: the patch prompt byte-extends document + draft; the
    # forced JSON shape rides json_schema (decoding-only, prompt untouched).
    assert llm_mock.calls == []
    raw = llm_mock.raw_calls[-1]
    assert raw["prompt"].startswith(ctx + flagged)
    assert raw["params"]["json_schema"]["properties"]["patches"]


async def test_patch_text_mode_assisted_rerenders_generation(client, llm_mock):
    await _seed_phrase(client)
    await _activate_text_endpoint(client)
    did = (await client.post("/api/documents", json={})).json()["id"]
    flagged = f"She felt {_BANNED} at once."
    ctx = "### USER: keep going\nThe last prose line"
    llm_mock.enqueue_raw(json.dumps({"patches": []}))

    body = (await client.post(f"/api/documents/{did}/patch", json={"draft": flagged, "context": ctx, "assisted": True})).json()
    assert body["skipped"] is None

    # The patch re-runs the EXACT generation render (same parsed messages +
    # open prefill) and byte-extends it with the draft core.
    gen_messages, prefill = build_generation_messages(ctx, assisted=True, completion_mode="text")
    assert llm_mock.render_calls == [{"messages": gen_messages, "prefill": prefill, "reasoning": False}]
    assert llm_mock.raw_calls[-1]["prompt"].startswith(f"<render:{len(gen_messages)}:{prefill}>{flagged}")
