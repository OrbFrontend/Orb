"""The HTTP body the client actually sends, over a mock transport.

The stop sequence is the one part of a completion request that belongs to the
CALLER'S WEIGHTS rather than to llama-server: a stop token is a property of a
checkpoint's chat template. This pins both halves of that — what a caller with
a stop token sends, and that a caller without one sends a body with no ``stop``
key at all rather than an empty list.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.inference.local_models.llama_server import client as C

pytestmark = pytest.mark.asyncio

_PROFILE = C.LaunchProfile(
    model_id="test",
    model_path="/models/test.gguf",
    alias="test-feature",
    gpu_layers=0,
    ctx_size=1280,
    parallel=1,
    http_threads=6,
)


def _client(monkeypatch, sent: list[dict]) -> C.LlamaServerClient:
    monkeypatch.setattr(C.binary_module, "supports_flag", lambda _binary, _flag: False)
    monkeypatch.setattr(C, "_free_port", lambda: 12345)
    server = C.LlamaServerClient(_PROFILE, C.Path("llama-server"))

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        body = 'data: {"content": "rewritten", "stop": true, "stop_type": "word"}\n\n'
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    server._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:12345")
    return server


async def test_completion_body_carries_the_callers_stop_sequence(monkeypatch):
    sent: list[dict] = []
    server = _client(monkeypatch, sent)

    text, stopped = await server.generate("prompt", n_predict=64, temperature=0.9, top_p=0.9, stop=("<|im_end|>",))

    assert (text, stopped) == ("rewritten", True)
    assert sent[0]["stop"] == ["<|im_end|>"]
    assert sent[0]["cache_prompt"] is True


async def test_a_caller_with_no_stop_sequence_sends_no_stop_key(monkeypatch):
    sent: list[dict] = []
    server = _client(monkeypatch, sent)

    await server.generate("prompt", n_predict=64, temperature=0.9, top_p=0.9)

    assert "stop" not in sent[0]


async def test_the_older_stopped_flags_still_read_as_a_finished_generation(monkeypatch):
    """Newer builds report ``stop_type``; older ones report three booleans.
    Either way the question is whether it ended or ran out of budget."""
    monkeypatch.setattr(C.binary_module, "supports_flag", lambda _binary, _flag: False)
    monkeypatch.setattr(C, "_free_port", lambda: 12345)
    server = C.LlamaServerClient(_PROFILE, C.Path("llama-server"))

    def handler(_request: httpx.Request) -> httpx.Response:
        body = 'data: {"content": "half a sen", "stop": true, "stopped_eos": false, "stopped_word": false}\n\n'
        return httpx.Response(200, text=body, headers={"Content-Type": "text/event-stream"})

    server._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:12345")

    assert await server.generate("prompt", n_predict=8, temperature=0.9, top_p=0.9) == ("half a sen", False)
