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


async def test_complete_raw_body_carries_n_probs_when_passed():
    client = _client()
    captured: dict = {}

    async def fake_stream(url, body):
        captured["body"] = body
        yield {"content": "x", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._stream_completion = fake_stream  # type: ignore[method-assign]

    await _drain(client.complete_raw("p", "m", n_probs=10))
    assert captured["body"]["n_probs"] == 10
    assert captured["body"]["post_sampling_probs"] is True


async def test_complete_raw_body_omits_n_probs_when_absent():
    client = _client()
    captured: dict = {}

    async def fake_stream(url, body):
        captured["body"] = body
        yield {"content": "x", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._stream_completion = fake_stream  # type: ignore[method-assign]

    await _drain(client.complete_raw("p", "m"))
    assert "n_probs" not in captured["body"]
    assert "post_sampling_probs" not in captured["body"]


async def test_complete_raw_interleaves_token_probs_chunks():
    client = _client()

    async def fake_stream(url, body):
        yield {
            "content": " Paris",
            "stop": False,
            "completion_probabilities": [
                {
                    "token": " Paris",
                    "prob": 0.86,
                    "top_probs": [{"token": " Paris", "prob": 0.86}, {"token": " own", "prob": 0.1}],
                }
            ],
        }
        yield {"content": "", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._stream_completion = fake_stream  # type: ignore[method-assign]

    events = await _drain(client.complete_raw("p", "m", n_probs=10))
    # content delta precedes its token_probs frame; the frame carries the normalized shape.
    types = [e["type"] for e in events]
    assert types[0] == "content" and types[1] == "token_probs"
    probs = [e for e in events if e["type"] == "token_probs"]
    assert probs == [
        {"type": "token_probs", "token": " Paris", "prob": 0.86, "top": [{"t": " Paris", "p": 0.86}, {"t": " own", "p": 0.1}]}
    ]


async def test_complete_raw_no_token_probs_when_server_omits_them():
    client = _client()

    async def fake_stream(url, body):
        # Server ignored n_probs (old build) → no completion_probabilities field.
        yield {"content": "hi", "stop": False}
        yield {"content": "", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._stream_completion = fake_stream  # type: ignore[method-assign]

    events = await _drain(client.complete_raw("p", "m", n_probs=10))
    assert not any(e["type"] == "token_probs" for e in events)
