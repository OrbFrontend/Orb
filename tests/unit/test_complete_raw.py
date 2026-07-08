"""Unit tests for LLMClient.complete_raw (raw text-completion transport).

Patches the documented ``_stream_completion`` HTTP seam so no sockets are
touched. complete_raw is the Document-mode continuation path: a bare prompt
string POSTed to llama.cpp ``/completion`` with no chat template and no
think-splitting.
"""

from __future__ import annotations

from backend.inference.client import LLMClient


def _client() -> LLMClient:
    return LLMClient("http://x/v1", completion_mode="text")


async def _drain(agen):
    return [e async for e in agen]


async def test_complete_raw_body_shape_and_verbatim_prompt():
    client = _client()
    captured: dict = {}

    async def fake_stream(url, body):
        captured["url"] = url
        captured["body"] = body
        yield {"content": "world", "stop": True, "tokens_evaluated": 3, "tokens_predicted": 1, "timings": {"prompt_n": 2}}

    client._stream_completion = fake_stream  # type: ignore[method-assign]

    events = await _drain(client.complete_raw("hello ", "m", max_tokens=64, repetition_penalty=1.1, temperature=0.7))

    # Native /completion, prompt sent verbatim, stream on, allowlist remap, cache_prompt.
    assert captured["url"] == "http://x/completion"
    body = captured["body"]
    assert body["prompt"] == "hello "
    assert body["stream"] is True
    assert body["n_predict"] == 64  # max_tokens -> n_predict
    assert body["repeat_penalty"] == 1.1  # repetition_penalty -> repeat_penalty
    assert body["temperature"] == 0.7
    assert body["cache_prompt"] is True
    assert "max_tokens" not in body and "repetition_penalty" not in body

    done = events[-1]
    assert done["type"] == "done"
    assert done["message"]["content"] == "world"
    assert done["usage"]["prompt_tokens"] == 3
    assert done["usage"]["completion_tokens"] == 1


async def test_complete_raw_streams_content_deltas_no_think_split():
    client = _client()

    async def fake_stream(url, body):
        # A literal <think> tag must arrive as CONTENT — raw mode has no channel.
        for piece in ["<think>", "not reasoning", "</think> tail"]:
            yield {"content": piece, "stop": False}
        yield {"content": "", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 3, "timings": {"prompt_n": 1}}

    client._stream_completion = fake_stream  # type: ignore[method-assign]

    events = await _drain(client.complete_raw("p", "m"))
    kinds = {e["type"] for e in events}
    assert "reasoning" not in kinds  # never splits a think channel
    content = "".join(e["delta"] for e in events if e["type"] == "content")
    assert content == "<think>not reasoning</think> tail"
    assert events[-1]["message"]["content"] == content
