"""Benchmark integrity checks, including parity with the native Editor boundary."""

import asyncio
import json

import httpx
import pytest

from backend.database.seeds import DEFAULT_SETTINGS
from backend.inference import CachedBase, LLMClient
from backend.pipeline.passes.editor.editor import editor_pass
from scripts.bench.auditor import (
    DEFAULT_TOGGLES,
    PHRASE_BANK,
    audit_bound_file,
    contextual_audit,
    create_app,
    model_report,
)
from scripts.bench.recorder import Recorder, response_facts


@pytest.mark.parametrize(
    ("draft", "history", "user"),
    [
        ("Mara sets the map on the table.", [], "I sit down."),
        ("Her voice dripped with honeyed sweetness. It was a testament to her skill.", [], "I listen."),
        ('"You packed the brass compass?"', [], '"I packed the brass compass."'),
        (
            "She opens the blue cabinet. She lifts the paper packet.",
            [{"role": "assistant", "content": "She sorts the paper. She opens the desk. She lifts the lid."}],
            "I wait.",
        ),
        (
            "The brass compass rests beside the ledger.",
            [{"role": "assistant", "content": "The brass compass rests beside the ledger."}] * 3,
            "I watch.",
        ),
        (
            "Mara sets the map on the table.",
            [{"role": "assistant", "content": "Her voice dripped with honeyed sweetness."}] * 24,
            "I sit down.",
        ),
    ],
    ids=["clean", "banned", "echo", "contextual-openers", "contextual-repetition", "window"],
)
async def test_auditor_matches_actual_editor(draft, history, user):
    result = contextual_audit(draft, history, user)
    # The actual Editor runs its scanners and builds its report. Empty tools
    # prevent generation after the audit; no scanner or private helper is mocked.
    base = CachedBase(prefix=tuple(history), tools=(), model="benchmark-fixture")
    client = LLMClient("http://127.0.0.1:1/v1")
    events = [
        event
        async for event in editor_pass(
            client, base, user, draft, {**DEFAULT_SETTINGS, "editor_audit_toggles": DEFAULT_TOGGLES}, PHRASE_BANK
        )
    ]
    done = events[-1]
    assert done["type"] == "done"
    assert done["debug"] == f"Initial audit ({result['total_issues']} issues):\n{result['numbered_report']}"


def test_bound_audit_saves_exact_bytes_and_rejects_escape(tmp_path):
    root = tmp_path / "native-run"
    (root / "output").mkdir(parents=True)
    raw = b"Mara folds the map.\r\n\r\nA gull lands.\n"
    (root / "output/main.md").write_bytes(raw)
    binding = tmp_path / "binding.json"
    binding.write_text(json.dumps({"run_id": "native", "workspace_root": str(root), "history": [], "user_message": "I wait."}))
    audit_bound_file(binding, tmp_path / "audits", "output/main.md")
    assert next((tmp_path / "audits").glob("*.md")).read_bytes() == raw
    (root / "output/main.md").unlink()
    outside = tmp_path / "private.md"
    outside.write_text("outside")
    (root / "output/main.md").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        audit_bound_file(binding, tmp_path / "audits", "output/main.md")


async def test_mcp_discovery_and_file_call(tmp_path):
    (tmp_path / "output").mkdir()
    (tmp_path / "output/main.md").write_text("Mara folds the map.")
    binding = tmp_path / "binding.json"
    binding.write_text(
        json.dumps({"run_id": "native", "workspace_root": str(tmp_path), "history": [], "user_message": "I wait."})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(binding, tmp_path / "audits")), base_url="http://test"
    ) as client:
        initialized = await client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}
        )
        assert initialized.json()["result"]["protocolVersion"] == "2025-03-26"
        tools = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert tools.json()["result"]["tools"][0]["name"] == "audit_draft"
        called = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "audit_draft", "arguments": {"path": "output/main.md"}},
            },
        )
        result = called.json()["result"]
        assert not result["isError"]
        assert "no issues found" in result["content"][0]["text"]
        assert next((tmp_path / "audits").glob("*.json")).exists()


def test_model_sees_the_editor_report_and_its_rules_not_raw_json():
    result = contextual_audit("Her voice dripped with honeyed sweetness. It was a testament to her skill.", [], "I listen.")
    text = model_report(result)
    assert result["total_issues"] and "patch each by its exact text" in text and "[id]" not in text
    assert "For banned phrases:" in text and '"targets"' not in text


@pytest.mark.parametrize("fail_after_first", [False, True])
async def test_recorder_preserves_bytes_streaming_and_failures(tmp_path, fail_after_first):
    first_forwarded = asyncio.Event()
    first = b'data: {"choices":[{"delta":{"content":"Mara"}}]}\n\n'
    last = b'data: {"usage":{"prompt_tokens":23,"completion_tokens":4}}\n\ndata: [DONE]\n\n'
    request_body = b'{ "messages": [], "stream": true }\n'

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield first
            # A buffering recorder deadlocks here: second data is not produced
            # until its first data has already been forwarded downstream.
            await asyncio.wait_for(first_forwarded.wait(), timeout=2)
            if fail_after_first:
                raise httpx.ReadError("upstream disconnected")
            yield last

    async def upstream(request):
        assert request.content == request_body
        assert request.url.path == "/v1/chat/completions"
        assert request.url.query == b"sample=one"
        assert request.headers["authorization"] == "Bearer local-test"
        return httpx.Response(201, headers={"content-type": "text/event-stream"}, stream=Stream())

    recorder = Recorder("http://upstream", tmp_path, transport=httpx.MockTransport(upstream))
    messages = []

    async def receive():
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message):
        messages.append(message)
        if message.get("body") == first:
            first_forwarded.set()

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "raw_path": b"/v1/chat/completions",
        "query_string": b"sample=one",
        "headers": [(b"authorization", b"Bearer local-test"), (b"content-type", b"application/json")],
    }
    try:
        if fail_after_first:
            with pytest.raises(httpx.ReadError):
                await recorder(scope, receive, send)
        else:
            await recorder(scope, receive, send)
    finally:
        await recorder.client.aclose()
    call_dir = next(tmp_path.iterdir())
    assert (call_dir / "request.bin").read_bytes() == request_body
    expected = first if fail_after_first else first + last
    assert (call_dir / "response.bin").read_bytes() == expected
    assert b"".join(msg.get("body", b"") for msg in messages) == expected
    assert messages[0]["status"] == 201
    metadata = json.loads((call_dir / "metadata.json").read_text())
    assert metadata["upstream_complete"] == (not fail_after_first)
    assert metadata["downstream_complete"] == (not fail_after_first)
    assert "Bearer local-test" not in (call_dir / "metadata.json").read_text()
    if not fail_after_first:
        assert metadata["usage"]["prompt_tokens"] == 23
        assert "cached_tokens" not in metadata["usage"]
        assert metadata["ingress_ns"] <= metadata["upstream_start_ns"] <= metadata["first_data_ns"] <= metadata["stream_end_ns"]
    else:
        assert messages[-1].get("more_body") is True


def test_missing_cache_is_not_zero():
    assert response_facts(b'{"usage":{"prompt_tokens":20}}') == {"usage": {"prompt_tokens": 20}}
    assert response_facts(b"data: [DONE]\n\n") == {}
