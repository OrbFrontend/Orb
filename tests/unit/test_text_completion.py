"""Unit tests for text-completion mode.

The leaf (backend/inference/text_completion.py) is pure, so most tests need no
HTTP mocking. The handful of client-level tests patch LLMClient's three HTTP
seams (_apply_template, _fetch_chat_template, _stream_completion) — no sockets,
no httpx faking.
"""

from __future__ import annotations

import httpx

from backend.inference import text_completion as tc
from backend.inference.client import LLMClient, parse_tool_calls, reasoning_cfg

GEMMA_OPEN, GEMMA_CLOSE, GEMMA_DISABLE = tc._GEMMA4


# ── Splitter ────────────────────────────────────────────────────────────────


def _run(splitter: tc.ThinkSplitter, chunks: list[str]) -> tuple[str, str]:
    """Feed *chunks* + flush; return (reasoning, content) concatenations."""
    reasoning, content = [], []
    for ch in chunks:
        for kind, text in splitter.feed(ch):
            (reasoning if kind == "reasoning" else content).append(text)
    for kind, text in splitter.flush():
        (reasoning if kind == "reasoning" else content).append(text)
    return "".join(reasoning), "".join(content)


def test_splitter_gemma_open_tag_split_across_three_chunks():
    # The live-observed split: '<|channel>' + 'thought' + '\n' arrive separately.
    r, c = _run(
        tc.ThinkSplitter(tc._GEMMA4),
        ["<|channel>", "thought", "\n", "The user", " said hi", "<channel|>", "Hello", "!"],
    )
    assert r == "The user said hi"
    assert c == "Hello!"


def test_splitter_gemma_close_tag_split_across_chunks():
    r, c = _run(tc.ThinkSplitter(tc._GEMMA4), [GEMMA_OPEN, "abc", "<channel", "|>Hi"])
    assert r == "abc"
    assert c == "Hi"


def test_splitter_think_pair():
    r, c = _run(tc.ThinkSplitter(tc._THINK), ["<think>", "reason", "</think>", "answer"])
    assert r == "reason"
    assert c == "answer"


def test_splitter_non_thinking_passthrough():
    # Empty tags → everything is content, from the first byte.
    r, c = _run(tc.ThinkSplitter(tc._NONE), ["hello ", "world"])
    assert r == ""
    assert c == "hello world"


def test_splitter_reasoning_on_but_no_channel_is_all_content():
    # Model never opens a thought channel despite reasoning-on → all content.
    r, c = _run(tc.ThinkSplitter(tc._GEMMA4), ["Just ", "answering."])
    assert r == ""
    assert c == "Just answering."


def test_splitter_flush_drains_mid_reasoning_tail_as_reasoning():
    # Truncated mid-span with a held partial close tag → flushed as reasoning.
    r, c = _run(tc.ThinkSplitter(tc._GEMMA4), [GEMMA_OPEN, "text", "<chan"])
    assert r == "text<chan"
    assert c == ""


def test_splitter_flush_drains_pre_state_tail_as_content():
    # A never-completed open tag at EOS is provisional content.
    r, c = _run(tc.ThinkSplitter(tc._GEMMA4), ["<|chan"])
    assert r == ""
    assert c == "<|chan"


# ── Tag sniff ordering ────────────────────────────────────────────────────────


def test_think_tags_channel_wins_over_think():
    assert tc.think_tags_from_template("...<|channel>thought... <think>...") == tc._GEMMA4


def test_think_tags_think_pair():
    assert tc.think_tags_from_template("...<think>...</think>...") == tc._THINK


def test_think_tags_none_for_non_thinking():
    assert tc.think_tags_from_template("plain jinja no markers") == tc._NONE


async def test_get_think_tags_caches_successful_sniff():
    tc._tag_cache.clear()
    calls = []

    async def fetch():
        calls.append(1)
        return "<|channel>thought here"

    assert await tc.get_think_tags("rootA", fetch) == tc._GEMMA4
    assert await tc.get_think_tags("rootA", fetch) == tc._GEMMA4
    assert len(calls) == 1  # cached; fetched once


async def test_get_think_tags_does_not_cache_failed_sniff():
    tc._tag_cache.clear()
    calls = []

    async def fetch():
        calls.append(1)
        return ""  # /props failed → empty

    await tc.get_think_tags("rootB", fetch)
    await tc.get_think_tags("rootB", fetch)
    assert len(calls) == 2  # retried; failure not cached


# ── Param remap ──────────────────────────────────────────────────────────────


def test_build_completion_params_remaps_and_drops():
    out = tc.build_completion_params(
        {
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 40,
            "min_p": 0.05,
            "max_tokens": 512,
            "repetition_penalty": 1.1,
            # dropped chat-only keys:
            "reasoning": {"enabled": False},
            "chat_template_kwargs": {"enable_thinking": False},
            "stream_options": {"include_usage": True},
            "prefill": "x",
        }
    )
    assert out == {
        "cache_prompt": True,
        "temperature": 0.7,
        "top_p": 0.9,
        "top_k": 40,
        "min_p": 0.05,
        "n_predict": 512,
        "repeat_penalty": 1.1,
    }


# ── Usage synthesis (F8) ──────────────────────────────────────────────────────


def test_synthesize_usage():
    usage = tc.synthesize_usage({"tokens_evaluated": 46, "tokens_predicted": 12, "timings": {"prompt_n": 5}})
    assert usage["prompt_tokens"] == 46
    assert usage["completion_tokens"] == 12
    assert usage["total_tokens"] == 58
    assert usage["prompt_tokens_details"]["cached_tokens"] == 41  # 46 - 5


def test_synthesize_usage_never_negative_cache():
    usage = tc.synthesize_usage({"tokens_evaluated": 3, "tokens_predicted": 1, "timings": {"prompt_n": 9}})
    assert usage["prompt_tokens_details"]["cached_tokens"] == 0


# ── Forced-call done message ──────────────────────────────────────────────────


def test_forced_tool_message_survives_parse_tool_calls():
    msg = tc.forced_tool_message("rate", '{"mood":"happy","score":3}')
    assert msg["content"] == ""
    # The raw JSON-string arguments flow through the existing json.loads path.
    assert parse_tool_calls(msg) == [{"name": "rate", "arguments": {"mood": "happy", "score": 3}}]


# ── forced_schema lookup ──────────────────────────────────────────────────────


def test_forced_schema_looks_up_by_name():
    tools = [
        {"type": "function", "function": {"name": "a", "parameters": {"type": "object", "x": 1}}},
        {"type": "function", "function": {"name": "b", "parameters": {"type": "object", "y": 2}}},
    ]
    choice = {"type": "function", "function": {"name": "b"}}
    assert tc.forced_schema(tools, choice) == {"type": "object", "y": 2}


def test_forced_schema_none_for_non_forced():
    tools = [{"type": "function", "function": {"name": "a", "parameters": {}}}]
    assert tc.forced_schema(tools, "auto") is None
    assert tc.forced_schema(tools, "required") is None
    assert tc.forced_schema(tools, None) is None
    assert tc.forced_schema(None, {"type": "function", "function": {"name": "a"}}) is None


# ── Image-part detection ──────────────────────────────────────────────────────


def test_has_image_parts():
    assert tc.has_image_parts([{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}])
    assert not tc.has_image_parts([{"role": "user", "content": "plain text"}])
    assert not tc.has_image_parts([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])


# ── reasoning flag ────────────────────────────────────────────────────────────


def test_reasoning_enabled_reads_reasoning_cfg():
    assert tc.reasoning_enabled(reasoning_cfg(True)) is True
    assert tc.reasoning_enabled(reasoning_cfg(False)) is False
    assert tc.reasoning_enabled({}) is True  # default on


# ── Client-level wiring (patched HTTP seams) ──────────────────────────────────


def _text_client() -> LLMClient:
    tc._tag_cache.clear()
    return LLMClient("http://x/v1", completion_mode="text")


async def _drain(agen):
    return [e async for e in agen]


async def test_complete_text_forced_call_end_to_end():
    client = _text_client()

    async def fake_apply(root, msgs):
        return "PROMPT"

    async def fake_props(root):
        return "<|channel>thought"

    async def fake_stream(url, body):
        for piece in ['{"mood"', ':"happy"', ',"score":1}']:
            yield {"content": piece, "stop": False}
        yield {"content": "", "stop": True, "tokens_evaluated": 10, "tokens_predicted": 5, "timings": {"prompt_n": 4}}

    client._apply_template = fake_apply  # type: ignore[method-assign]
    client._fetch_chat_template = fake_props  # type: ignore[method-assign]
    client._stream_completion = fake_stream  # type: ignore[method-assign]

    tools = [{"type": "function", "function": {"name": "rate", "parameters": {"type": "object"}}}]
    choice = {"type": "function", "function": {"name": "rate"}}
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", tools=tools, tool_choice=choice)
    )

    assert not any(e["type"] == "content" for e in events)  # forced → no content deltas
    done = events[-1]
    assert parse_tool_calls(done["message"]) == [{"name": "rate", "arguments": {"mood": "happy", "score": 1}}]
    assert done["usage"]["prompt_tokens"] == 10
    assert done["usage"]["prompt_tokens_details"]["cached_tokens"] == 6


async def test_complete_text_disable_suffix_toggles_with_reasoning():
    client = _text_client()
    captured: dict = {}

    async def fake_apply(root, msgs):
        return "BASE"

    async def fake_props(root):
        return "<|channel>thought"

    async def fake_stream(url, body):
        captured["prompt"] = body["prompt"]
        yield {"content": "hi", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._apply_template = fake_apply  # type: ignore[method-assign]
    client._fetch_chat_template = fake_props  # type: ignore[method-assign]
    client._stream_completion = fake_stream  # type: ignore[method-assign]

    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(False)))
    assert captured["prompt"] == "BASE" + GEMMA_DISABLE

    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True)))
    assert captured["prompt"] == "BASE"  # reasoning on → no suffix


async def test_complete_text_prefill_appends_assistant_message():
    client = _text_client()
    captured: dict = {}

    async def fake_apply(root, msgs):
        captured["msgs"] = list(msgs)
        return "P"

    async def fake_props(root):
        return ""  # non-thinking; no suffix regardless

    async def fake_stream(url, body):
        captured["prompt"] = body["prompt"]
        yield {"content": "x", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._apply_template = fake_apply  # type: ignore[method-assign]
    client._fetch_chat_template = fake_props  # type: ignore[method-assign]
    client._stream_completion = fake_stream  # type: ignore[method-assign]

    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", prefill="Once upon"))
    assert captured["msgs"][-1] == {"role": "assistant", "content": "Once upon"}


async def test_complete_text_apply_template_error_falls_back_to_chat():
    client = _text_client()

    async def boom(root, msgs):
        raise httpx.ConnectError("nope")

    async def fake_chat(messages, model, tools=None, tool_choice=None, **params):
        yield {"type": "content", "delta": "CHAT"}
        yield {"type": "done", "message": {"content": "CHAT"}, "usage": None}

    client._apply_template = boom  # type: ignore[method-assign]
    client._complete_chat = fake_chat  # type: ignore[method-assign]

    events = await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m"))
    assert events[-1]["message"]["content"] == "CHAT"


async def test_image_call_routes_through_chat_transport():
    client = _text_client()

    async def fake_chat(messages, model, tools=None, tool_choice=None, **params):
        yield {"type": "done", "message": {"content": "CHAT"}, "usage": None}

    async def must_not_run(*a, **k):
        raise AssertionError("text transport used for an image-bearing call")
        yield  # pragma: no cover — makes this an async generator

    client._complete_chat = fake_chat  # type: ignore[method-assign]
    client._complete_text = must_not_run  # type: ignore[method-assign]

    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}]
    events = await _drain(client.complete(messages=msgs, model="m"))
    assert events[-1]["message"]["content"] == "CHAT"


async def test_chat_transport_drops_prefill():
    client = LLMClient("http://x/v1", completion_mode="chat")
    captured: dict = {}

    async def fake_chat(messages, model, tools=None, tool_choice=None, **params):
        captured["params"] = params
        yield {"type": "done", "message": {}, "usage": None}

    client._complete_chat = fake_chat  # type: ignore[method-assign]
    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", prefill="X"))
    assert "prefill" not in captured["params"]
