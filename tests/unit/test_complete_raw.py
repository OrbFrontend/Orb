"""Unit tests for LLMClient.complete_raw (raw text-completion transport).

Patches the documented ``_stream_completion`` HTTP seam so no sockets are touched. complete_raw is the Document-mode
continuation path: a bare prompt string POSTed to llama.cpp ``/completion`` with no chat template and no think-splitting.
"""

import pytest

from backend.inference.client import LLMClient

_STOP = {"content": "", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}


async def _raw(*chunks: dict, prompt: str = "p", **params) -> tuple[list[dict], dict]:
    """Run complete_raw against a stream of *chunks* (then a stop); return its events and what was POSTed."""
    client = LLMClient("http://x/v1", completion_mode="text")
    captured: dict = {}

    async def fake_stream(url, body):
        captured.update(url=url, body=body)
        for chunk in chunks:
            yield chunk

    client._stream_completion = fake_stream  # type: ignore[method-assign]
    return [e async for e in client.complete_raw(prompt, "m", **params)], captured


async def test_complete_raw_body_shape_and_verbatim_prompt():
    final = {"content": "world", "stop": True, "tokens_evaluated": 3, "tokens_predicted": 1, "timings": {"prompt_n": 2}}
    events, captured = await _raw(final, prompt="hello ", max_tokens=64, repetition_penalty=1.1, temperature=0.7)
    # Native /completion, prompt sent verbatim, stream on, allowlist remap, cache_prompt.
    assert captured["url"] == "http://x/completion"
    body = captured["body"]
    assert (body["prompt"], body["stream"], body["temperature"], body["cache_prompt"]) == ("hello ", True, 0.7, True)
    assert body["n_predict"] == 64  # max_tokens -> n_predict
    assert body["repeat_penalty"] == 1.1  # repetition_penalty -> repeat_penalty
    assert "max_tokens" not in body and "repetition_penalty" not in body

    done = events[-1]
    assert done["type"] == "done"
    assert done["message"]["content"] == "world"
    assert (done["usage"]["prompt_tokens"], done["usage"]["completion_tokens"]) == (3, 1)


async def test_complete_raw_streams_content_deltas_no_think_split():
    # A literal <think> tag must arrive as CONTENT -- raw mode has no channel.
    events, _ = await _raw(
        *({"content": piece, "stop": False} for piece in ["<think>", "not reasoning", "</think> tail"]), _STOP
    )
    assert "reasoning" not in {e["type"] for e in events}
    content = "".join(e["delta"] for e in events if e["type"] == "content")
    assert content == "<think>not reasoning</think> tail"
    assert events[-1]["message"]["content"] == content


@pytest.mark.parametrize(
    "params,present,absent",
    [
        ({"n_probs": 10}, {"n_probs": 10, "post_sampling_probs": True}, ()),
        ({}, {}, ("n_probs", "post_sampling_probs", "json_schema", "grammar")),
        # Doc-mode patch path: json_schema constrains decoding only; the prompt is still the verbatim string.
        ({"json_schema": {"type": "object"}}, {"json_schema": {"type": "object"}, "prompt": "p"}, ("grammar",)),
        ({"grammar": 'root ::= "x"', "json_schema": {"type": "object"}}, {"grammar": 'root ::= "x"'}, ("json_schema",)),
    ],
)
async def test_complete_raw_body_fields(params, present, absent):
    _, captured = await _raw(_STOP, **params)
    body = captured["body"]
    assert {key: body[key] for key in present} == present
    assert not set(absent) & set(body)


async def test_complete_raw_interleaves_token_probs_chunks():
    probs = [
        {"token": " Paris", "prob": 0.86, "top_probs": [{"token": " Paris", "prob": 0.86}, {"token": " own", "prob": 0.1}]}
    ]
    events, _ = await _raw({"content": " Paris", "stop": False, "completion_probabilities": probs}, _STOP, n_probs=10)
    # The content delta precedes its token_probs frame; the frame carries the normalized shape.
    assert [e["type"] for e in events][:2] == ["content", "token_probs"]
    assert [e for e in events if e["type"] == "token_probs"] == [
        {"type": "token_probs", "token": " Paris", "prob": 0.86, "top": [{"t": " Paris", "p": 0.86}, {"t": " own", "p": 0.1}]}
    ]
    # A server that ignored n_probs (old build) sends no completion_probabilities field.
    events, _ = await _raw({"content": "hi", "stop": False}, _STOP, n_probs=10)
    assert not any(e["type"] == "token_probs" for e in events)
