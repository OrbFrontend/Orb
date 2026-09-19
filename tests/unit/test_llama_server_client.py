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


# --- the token-id path, which an audio model cannot do without ---------------
#
# Spark-TTS's speaker and audio tokens are all typed CONTROL in the GGUF, and
# `--special` defaults to false, so `content` arrives EMPTY for a completion
# that is entirely audio. A text-based reader does not fail against that; it
# silently returns nothing. These pin both halves of the alternative: an int
# array going out, and `tokens` coming back.


def _token_client(monkeypatch, sent: list[dict], chunks: list[str]) -> C.LlamaServerClient:
    monkeypatch.setattr(C.binary_module, "supports_flag", lambda _binary, _flag: False)
    monkeypatch.setattr(C, "_free_port", lambda: 12345)
    server = C.LlamaServerClient(_PROFILE, C.Path("llama-server"))

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if request.url.path == "/tokenize":
            return httpx.Response(200, json={"tokens": [9001, 9002]})
        return httpx.Response(200, text="".join(chunks), headers={"Content-Type": "text/event-stream"})

    server._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:12345")
    return server


_AUDIO_STREAM = [
    'data: {"content": "", "tokens": [165151]}\n\n',
    'data: {"content": "", "tokens": [155761]}\n\n',
    'data: {"content": "", "tokens": [163952]}\n\n',
    'data: {"content": "", "tokens": [], "stop": true, "stop_type": "eos"}\n\n',
]


async def test_generate_tokens_sends_an_int_array_and_reads_the_token_ids(monkeypatch):
    sent: list[dict] = []
    server = _token_client(monkeypatch, sent, _AUDIO_STREAM)

    got, stopped = await server.generate_tokens([1, 2, 3], n_predict=64, temperature=0.8, top_p=0.95, top_k=50, seed=7)

    assert got == [165151, 155761, 163952]
    assert stopped is True
    body = sent[0]
    assert body["prompt"] == [1, 2, 3]  # not a string: no tokenizer round-trip
    assert body["return_tokens"] is True
    assert body["stream"] is True
    assert body["top_k"] == 50
    assert body["seed"] == 7


async def test_generate_tokens_omits_optional_sampling_keys(monkeypatch):
    """A caller that names neither must send the body a caller with no opinion
    would send, rather than pinning llama.cpp's own defaults from out here."""
    sent: list[dict] = []
    server = _token_client(monkeypatch, sent, _AUDIO_STREAM)

    await server.generate_tokens([1], n_predict=8, temperature=0.8, top_p=0.95)

    assert "top_k" not in sent[0]
    assert "seed" not in sent[0]
    assert "logit_bias" not in sent[0]


async def test_generate_tokens_bans_tokens_through_logit_bias(monkeypatch):
    """``false`` is llama-server's "never sample this", not a large negative bias."""
    sent: list[dict] = []
    server = _token_client(monkeypatch, sent, _AUDIO_STREAM)

    await server.generate_tokens([1], n_predict=1, temperature=0.8, top_p=0.95, banned=(151643, 151645))

    assert sent[0]["logit_bias"] == [[151643, False], [151645, False]]


async def test_generate_tokens_survives_a_build_that_batches_chunks(monkeypatch):
    """One id per chunk on b10549, but the field is an ARRAY; a future build
    batching them must not be silently truncated to its first element."""
    sent: list[dict] = []
    stream = [
        'data: {"tokens": [155761, 155762, 155763]}\n\n',
        'data: {"tokens": [155764], "stop": true, "stop_type": "limit"}\n\n',
    ]
    server = _token_client(monkeypatch, sent, stream)

    got, stopped = await server.generate_tokens([1], n_predict=4, temperature=0.8, top_p=0.95)

    assert got == [155761, 155762, 155763, 155764]
    assert stopped is False  # ran out of budget, did not end on its own


async def test_tokenize_defaults_to_not_parsing_specials(monkeypatch):
    """A character's line containing ``<|end_content|>`` is six characters of
    dialogue, not a control token. Honouring specials in user text is how that
    stops being true."""
    sent: list[dict] = []
    server = _token_client(monkeypatch, sent, _AUDIO_STREAM)

    assert await server.tokenize("hello") == [9001, 9002]
    assert sent[0] == {"content": "hello", "parse_special": False}

    await server.tokenize("<|task_tts|>", parse_special=True)
    assert sent[1]["parse_special"] is True
