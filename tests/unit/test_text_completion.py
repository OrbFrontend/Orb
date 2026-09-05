"""Unit tests for text-completion mode.

The leaf (backend/inference/text_completion.py) is pure, so most tests need no
HTTP mocking. The handful of client-level tests patch LLMClient's three HTTP
seams (_apply_template, _fetch_chat_template, _stream_completion) — no sockets,
no httpx faking.
"""

from __future__ import annotations

import httpx
import pytest

from backend.inference import text_completion as tc
from backend.inference.client import (
    LLMClient,
    _parse_chat_logprobs,
    parse_tool_calls,
    reasoning_cfg,
)
from backend.inference.retry import RetryPolicy

GEMMA_OPEN, GEMMA_CLOSE, GEMMA_DISABLE = tc._GEMMA4


# ── Splitter ────────────────────────────────────────────────────────────────


def _run(splitter: tc.Splitter, chunks: list[str]) -> tuple[str, str]:
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


def test_splitter_already_open_starts_in_reasoning():
    # Qwen3: prompt pre-opened <think>, so the stream has no leading open tag.
    r, c = _run(tc.ThinkSplitter(tc._THINK, already_open=True), ["reason", "</think>", "answer"])
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


def test_think_tags_minimax_namespaced_pair():
    assert tc.think_tags_from_template("...<mm:think>...</mm:think>...") == tc._MINIMAX


def test_think_tags_novel_namespace_derived():
    # The tag-pair family generalizes: any <ns:think> yields its own triple.
    assert tc.think_tags_from_template("...<seed:think>...") == (
        "<seed:think>",
        "</seed:think>",
        "<seed:think>\n\n</seed:think>\n\n",
    )


def test_think_tags_hunyuan_format_constructed():
    # Hunyuan builds the tag from a namespace var rather than writing it
    # literally; the sniff must resolve `.format(HYTK)` to see the real bytes.
    raw = "{%- set HYTK = ':opensource' %}{%- set think_begin_token = '<think{}>'.format(HYTK) %}{{ think_begin_token }}"
    assert tc.think_tags_from_template(raw) == (
        "<think:opensource>",
        "</think:opensource>",
        "<think:opensource>\n\n</think:opensource>\n\n",
    )


def test_think_tags_thinking_and_thought_variants():
    assert tc.think_tags_from_template("...<thinking>...")[0:2] == ("<thinking>", "</thinking>")
    assert tc.think_tags_from_template("...<thought>...")[0:2] == ("<thought>", "</thought>")


def test_think_tags_prose_word_does_not_match():
    # Bare words without the tag brackets are not a reasoning span.
    assert tc.think_tags_from_template("think about thought and thinking") == tc._NONE


def test_splitter_minimax_pair():
    r, c = _run(tc.ThinkSplitter(tc._MINIMAX), ["<mm:think>", "reason", "</mm:think>", "answer"])
    assert r == "reason"
    assert c == "answer"


async def test_get_reasoning_format_re_sniffs_every_call():
    # A model swap must be picked up on the next call.
    templates = ["<think>...</think>", "<|channel>thought here"]  # Qwen, then a swap to Gemma

    async def fetch():
        return templates.pop(0)

    assert (await tc.get_reasoning_format(fetch)).tags == tc._THINK
    assert (await tc.get_reasoning_format(fetch)).tags == tc._GEMMA4


async def test_get_reasoning_format_failed_sniff_falls_back_to_none():
    async def fetch():
        return ""  # /props failed → empty

    fmt = await tc.get_reasoning_format(fetch)
    assert fmt.tags == tc._NONE
    assert fmt.channel is False


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


def test_build_completion_params_n_probs_adds_post_sampling():
    out = tc.build_completion_params({"n_probs": 10})
    assert out["n_probs"] == 10
    assert out["post_sampling_probs"] is True


def test_build_completion_params_n_probs_absent_by_default():
    # No n_probs → neither field appears (old servers, probs toggle off).
    out = tc.build_completion_params({"temperature": 0.7})
    assert "n_probs" not in out
    assert "post_sampling_probs" not in out


def test_build_completion_params_n_probs_ignores_nonpositive_and_bool():
    # 0, negatives, and the bool True (an int subclass) are not real requests.
    for bad in (0, -1, True, "10", None):
        out = tc.build_completion_params({"n_probs": bad})
        assert "n_probs" not in out, bad
        assert "post_sampling_probs" not in out, bad


# ── parse_token_probs: three llama.cpp shapes + garbage ───────────────────────


def test_parse_token_probs_post_sampling_shape():
    # Current shape: {token, prob, top_probs:[{token, prob}]} (linear probs).
    data = {
        "completion_probabilities": [
            {
                "token": " Paris",
                "prob": 0.86,
                "top_probs": [{"token": " Paris", "prob": 0.86}, {"token": " own", "prob": 0.1}],
            }
        ]
    }
    assert tc.parse_token_probs(data) == [
        {"token": " Paris", "prob": 0.86, "top": [{"t": " Paris", "p": 0.86}, {"t": " own", "p": 0.1}]}
    ]


def test_parse_token_probs_logprob_shape_exponentiated():
    # {token, logprob, top_logprobs:[{token, logprob}]} → math.exp to linear.
    import math

    data = {
        "completion_probabilities": [
            {"token": "x", "logprob": -0.1, "top_logprobs": [{"token": "x", "logprob": -0.1}, {"token": "y", "logprob": -2.0}]}
        ]
    }
    [rec] = tc.parse_token_probs(data)
    assert rec["token"] == "x"
    assert rec["prob"] == pytest.approx(math.exp(-0.1))
    assert rec["top"][0]["t"] == "x" and rec["top"][0]["p"] == pytest.approx(math.exp(-0.1))
    assert rec["top"][1]["p"] == pytest.approx(math.exp(-2.0))


def test_parse_token_probs_legacy_shape_derives_prob_from_alts():
    # Legacy {content, probs:[{tok_str, prob}]} — no top-level prob; the sampled
    # token's prob is read from the alternatives.
    data = {
        "completion_probabilities": [
            {"content": " the", "probs": [{"tok_str": " the", "prob": 0.7}, {"tok_str": " a", "prob": 0.2}]}
        ]
    }
    assert tc.parse_token_probs(data) == [
        {"token": " the", "prob": 0.7, "top": [{"t": " the", "p": 0.7}, {"t": " a", "p": 0.2}]}
    ]


def test_parse_token_probs_garbage_returns_empty_never_raises():
    assert tc.parse_token_probs({}) == []
    assert tc.parse_token_probs({"completion_probabilities": None}) == []
    assert tc.parse_token_probs({"completion_probabilities": "nope"}) == []
    assert tc.parse_token_probs({"completion_probabilities": [42, "x", None]}) == []
    # A record with no usable token string is skipped, not fatal.
    assert tc.parse_token_probs({"completion_probabilities": [{"prob": 0.5}]}) == []


def test_parse_token_probs_multiple_records_and_missing_alts():
    # More than one token in a chunk; a record with no alternatives keeps prob.
    data = {
        "completion_probabilities": [
            {"token": "a", "prob": 0.9, "top_probs": []},
            {"token": "b", "prob": 0.5},
        ]
    }
    assert tc.parse_token_probs(data) == [
        {"token": "a", "prob": 0.9, "top": []},
        {"token": "b", "prob": 0.5, "top": []},
    ]


# ── Chat-transport logprobs normalization ─────────────────────────────────────


def test_parse_chat_logprobs_exponentiates_to_linear():
    import math

    choice = {
        "logprobs": {
            "content": [
                {
                    "token": " the",
                    "logprob": -0.2,
                    "top_logprobs": [{"token": " the", "logprob": -0.2}, {"token": " a", "logprob": -1.6}],
                }
            ]
        }
    }
    [rec] = _parse_chat_logprobs(choice)
    assert rec["token"] == " the"
    assert rec["prob"] == pytest.approx(math.exp(-0.2))
    assert rec["top"][0] == {"t": " the", "p": pytest.approx(math.exp(-0.2))}
    assert rec["top"][1] == {"t": " a", "p": pytest.approx(math.exp(-1.6))}


def test_parse_chat_logprobs_absent_returns_empty():
    # Provider omitted logprobs entirely (graceful degrade → no popup).
    assert _parse_chat_logprobs({}) == []
    assert _parse_chat_logprobs({"logprobs": None}) == []
    assert _parse_chat_logprobs({"logprobs": {}}) == []
    assert _parse_chat_logprobs({"logprobs": {"content": None}}) == []


def test_parse_chat_logprobs_skips_malformed_records():
    choice = {
        "logprobs": {
            "content": [
                42,
                {"token": None, "logprob": -0.1},  # non-str token → skip
                {"token": "x"},  # no readable prob → degrades to 0.0
                {"token": "y", "logprob": -0.5, "top_logprobs": ["junk", {"token": "z", "logprob": -0.9}]},
            ]
        }
    }
    out = _parse_chat_logprobs(choice)
    assert len(out) == 2
    assert out[0] == {"token": "x", "prob": 0.0, "top": []}
    assert out[1]["token"] == "y"
    # The junk alternative is dropped; the valid one survives.
    assert out[1]["top"] == [{"t": "z", "p": pytest.approx(__import__("math").exp(-0.9))}]


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
    return LLMClient("http://x/v1", completion_mode="text")


async def _drain(agen):
    return [e async for e in agen]


def _wired_text_client(template="P", props="", pieces=("x",), captured=None, final=None) -> LLMClient:
    """Text client with deterministic /apply-template, /props and stream seams."""
    client = _text_client()
    captured = captured if captured is not None else {}

    async def fake_apply(root, msgs, chat_template_kwargs=None):
        captured["msgs"] = list(msgs)
        captured["ctk"] = chat_template_kwargs
        return template(msgs, chat_template_kwargs) if callable(template) else template

    async def fake_props(root):
        return props

    async def fake_stream(url, body):
        captured["body"] = body
        captured["prompt"] = body["prompt"]
        for piece in pieces:
            yield {"content": piece, "stop": False}
        yield final or {
            "content": "",
            "stop": True,
            "tokens_evaluated": 1,
            "tokens_predicted": 1,
            "timings": {"prompt_n": 1},
        }

    client._apply_template = fake_apply  # type: ignore[method-assign]
    client._fetch_chat_template = fake_props  # type: ignore[method-assign]
    client._stream_completion = fake_stream  # type: ignore[method-assign]
    return client


async def test_complete_text_forced_call_end_to_end():
    client = _wired_text_client(
        template="PROMPT",
        props="<|channel>thought",
        pieces=['{"mood"', ':"happy"', ',"score":1}'],
        final={"content": "", "stop": True, "tokens_evaluated": 10, "tokens_predicted": 5, "timings": {"prompt_n": 4}},
    )

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


async def test_complete_text_enable_thinking_delegated_to_template_no_manual_suffix():
    # The template owns reasoning on/off: the client forwards enable_thinking to
    # /apply-template and does NOT hand-append disable bytes (which double-opened
    # Qwen3's pre-opened <think>). The fake template echoes what it was told.
    captured: dict = {}
    client = _wired_text_client(
        # Mimic a template that closes an empty think block when thinking is off.
        template=lambda _msgs, ctk: "BASE" + ("" if (ctk or {}).get("enable_thinking", True) else GEMMA_DISABLE),
        props="<|channel>thought",
        pieces=["hi"],
        captured=captured,
    )

    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(False)))
    assert captured["ctk"] == {"enable_thinking": False, "thinking": False}  # both toggle aliases
    assert captured["prompt"] == "BASE" + GEMMA_DISABLE  # from the template, not the client

    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True)))
    assert captured["ctk"] == {"enable_thinking": True, "thinking": True}
    assert captured["prompt"] == "BASE"  # reasoning on → template renders no disable bytes


async def test_complete_text_primes_splitter_when_prompt_pre_opens_think():
    # Qwen3 case: template pre-opens <think> in the prompt, so the model stream
    # starts INSIDE reasoning (no leading <think>). The splitter must classify the
    # CoT as reasoning and only the post-</think> text as content.
    client = _wired_text_client(
        template="<|im_start|>assistant\n<think>\n",  # ends with the open tag
        props="<think>...</think>",  # sniffs to _THINK
        pieces=["Analyzing", " the ask.", "</think>", "\n\nSarah smiled."],
    )
    events_seen = await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True)))
    reasoning = "".join(e["delta"] for e in events_seen if e.get("type") == "reasoning")
    content = "".join(e["delta"] for e in events_seen if e.get("type") == "content")
    assert reasoning == "Analyzing the ask."
    assert content == "Sarah smiled."  # the template's "</think>\n\n" padding is trimmed
    assert "</think>" not in content  # the special token no longer leaks


async def test_complete_text_prefill_appends_assistant_message():
    captured: dict = {}
    client = _wired_text_client(captured=captured)  # non-thinking; no suffix regardless

    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", prefill="Once upon"))
    assert captured["msgs"][-1] == {"role": "assistant", "content": "Once upon"}
    assert captured["ctk"] is None  # prefill skips enable_thinking; the trailing turn governs it


async def test_complete_text_forced_prefill_prepends_arguments():
    # Editor prefill path: arguments = prompt-side prefill bytes + generated
    # remainder, so json.loads sees one complete object.
    client = _wired_text_client(pieces=['REPL"', "}]}"])

    tools = [{"type": "function", "function": {"name": "editor_apply_patch", "parameters": {"type": "object"}}}]
    choice = {"type": "function", "function": {"name": "editor_apply_patch"}}
    events = await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            tools=tools,
            tool_choice=choice,
            prefill='{"patches": [{"search": "old", "replace": "',
        )
    )
    assert parse_tool_calls(events[-1]["message"]) == [
        {"name": "editor_apply_patch", "arguments": {"patches": [{"search": "old", "replace": "REPL"}]}}
    ]


async def test_complete_text_pre_open_detected_from_bytes_even_when_reasoning_off():
    # Kimi K2 case: the template keys thinking off a boolean `thinking`, not the
    # `enable_thinking` we send, so a reasoning-OFF request still renders a
    # pre-opened <think>. The splitter must detect the pre-open from the rendered
    # bytes (not our flag) and route the CoT to reasoning instead of collapsing.
    client = _wired_text_client(
        template="<|im_assistant|>assistant<|im_middle|><think>",  # pre-opened despite off
        props="<think>...</think>",  # sniffs to _THINK
        pieces=["reasoning here", "</think>", "the answer"],
    )

    events = await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(False)))
    reasoning = "".join(e["delta"] for e in events if e.get("type") == "reasoning")
    content = "".join(e["delta"] for e in events if e.get("type") == "content")
    assert reasoning == "reasoning here"
    assert content == "the answer"
    assert "</think>" not in content  # no collapse, no leaked token despite reasoning=off


async def test_complete_text_grammar_overrides_json_schema():
    captured: dict = {}
    client = _wired_text_client(captured=captured)

    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    choice = {"type": "function", "function": {"name": "t"}}
    await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            tools=tools,
            tool_choice=choice,
            grammar='root ::= "x"',
        )
    )
    assert captured["body"]["grammar"] == 'root ::= "x"'
    assert "json_schema" not in captured["body"]


async def test_complete_text_json_schema_narrows_forced_grammar():
    # Per-fragment director steps: the caller-supplied json_schema replaces the
    # tool-derived one, constraining decoding without touching the prompt.
    captured: dict = {}
    client = _wired_text_client(pieces=["{}"], captured=captured)

    full = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    narrow = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    tools = [{"type": "function", "function": {"name": "t", "parameters": full}}]
    choice = {"type": "function", "function": {"name": "t"}}
    await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}], model="m", tools=tools, tool_choice=choice, json_schema=narrow
        )
    )
    assert captured["body"]["json_schema"] == narrow


async def test_chat_transport_drops_grammar():
    client = LLMClient("http://x/v1", completion_mode="chat")
    captured: dict = {}

    async def fake_chat(messages, model, tools=None, tool_choice=None, **params):
        captured["params"] = params
        yield {"type": "done", "message": {}, "usage": None}

    client._complete_chat = fake_chat  # type: ignore[method-assign]
    await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            grammar="root ::= x",
            json_schema={"type": "object"},
        )
    )
    assert "grammar" not in captured["params"]
    # json_schema is deliberately passed through: the chat transport consumes
    # it for structured forced calls and discards it otherwise.
    assert captured["params"]["json_schema"] == {"type": "object"}


async def test_complete_text_apply_template_error_falls_back_to_chat():
    client = _text_client()
    captured = {}

    async def boom(root, msgs, chat_template_kwargs=None):
        raise httpx.ConnectError("nope")

    async def fake_chat(messages, model, tools=None, tool_choice=None, **params):
        captured["tools"] = tools
        captured["tool_choice"] = tool_choice
        captured["params"] = params
        yield {"type": "content", "delta": "CHAT"}
        yield {"type": "done", "message": {"content": "CHAT"}, "usage": None}

    client._apply_template = boom  # type: ignore[method-assign]
    client._complete_chat = fake_chat  # type: ignore[method-assign]

    tools = [{"type": "function", "function": {"name": "rate", "parameters": {"type": "object"}}}]
    choice = {"type": "function", "function": {"name": "rate"}}
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", tools=tools, tool_choice=choice)
    )
    assert events[-1]["message"]["content"] == "CHAT"
    assert captured["tools"] == tools  # still sources the response_format schema
    assert captured["tool_choice"] == choice
    assert captured["params"]["tools_in_prompt"] is False


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


# ── Reasoning prefill ─────────────────────────────────────────────────────────


def test_reasoning_cfg_carries_prefill_only_when_reasoning_on():
    assert reasoning_cfg(True, "seed")["reasoning_prefill"] == "seed"
    assert "reasoning_prefill" not in reasoning_cfg(True)
    assert "reasoning_prefill" not in reasoning_cfg(True, "")
    assert "reasoning_prefill" not in reasoning_cfg(False, "seed")


async def test_reasoning_prefill_appends_to_pre_opened_think():
    # Qwen3 shape: the template already opened <think>, so only the seed text is
    # appended (no second open tag), and it is echoed as reasoning ahead of the
    # model's own deltas.
    captured: dict = {}
    client = _wired_text_client("<|im_start|>assistant\n<think>\n", "<think>...</think>", ["CoT", "</think>", "text"], captured)
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True, "I will think. "))
    )
    assert captured["prompt"] == "<|im_start|>assistant\n<think>\nI will think. "
    assert events[0] == {"type": "reasoning", "delta": "I will think. "}
    reasoning = "".join(e["delta"] for e in events if e.get("type") == "reasoning")
    content = "".join(e["delta"] for e in events if e.get("type") == "content")
    assert reasoning == "I will think. CoT"  # echo + the model's continuation
    assert content == "text"


async def test_reasoning_prefill_opens_think_when_template_does_not():
    # Gemma 4 shape: the template leaves the open tag to the model, so Orb injects it.
    captured: dict = {}
    client = _wired_text_client("BASE", "<|channel>thought", ["x"], captured)
    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True, "Seed.")))
    assert captured["prompt"] == "BASE" + GEMMA_OPEN + "Seed."


async def test_reasoning_prefill_ignored_when_reasoning_off():
    captured: dict = {}
    client = _wired_text_client("BASE", "<|channel>thought", ["x"], captured)
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(False, "Seed."))
    )
    assert captured["prompt"] == "BASE"
    assert not any(e["type"] == "reasoning" for e in events)


async def test_reasoning_prefill_ignored_on_non_thinking_template():
    captured: dict = {}
    client = _wired_text_client("BASE", "", ["x"], captured)  # props sniff → _NONE
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True, "Seed."))
    )
    assert captured["prompt"] == "BASE"
    assert not any(e["type"] == "reasoning" for e in events)


async def test_assistant_prefill_wins_over_reasoning_prefill():
    # The assistant prefill already owns the prompt tail (a trailing assistant
    # turn in render_prompt); the two cannot both.
    captured: dict = {}
    client = _wired_text_client("BASE", "<|channel>thought", ["x"], captured)
    events = await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            prefill="Once upon",
            **reasoning_cfg(True, "Seed."),
        )
    )
    assert captured["prompt"] == "BASE"
    assert not any(e["type"] == "reasoning" for e in events)


async def test_reasoning_prefill_preserves_retry_window():
    # The echo is emitted on the first streamed chunk, not before the POST, so a
    # transient failure is still a clean retry (no event yielded yet).
    captured: dict = {}
    client = _wired_text_client("BASE", "<|channel>thought", ["x"], captured)
    client.retry = RetryPolicy(count=1, delay=0)
    attempts = {"n": 0}
    real_stream = client._stream_completion

    async def flaky_stream(url, body):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("blip")
        async for data in real_stream(url, body):
            yield data

    client._stream_completion = flaky_stream  # type: ignore[method-assign]
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True, "Seed."))
    )
    assert attempts["n"] == 2  # retried
    assert events[0] == {"type": "reasoning", "delta": "Seed."}  # echoed exactly once
    assert sum(1 for e in events if e.get("delta") == "Seed.") == 1


# ── Leading-whitespace trim at the content seam ───────────────────────────────


def test_splitter_trims_template_padding_after_close_tag():
    # Qwen renders "</think>\n\n"; the blank line must not open the reply.
    r, c = _run(tc.ThinkSplitter(tc._THINK, already_open=True), ["reason", "</think>", "\n\n", "Sarah smiled."])
    assert r == "reason"
    assert c == "Sarah smiled."


def test_splitter_trims_when_padding_shares_the_close_chunk():
    r, c = _run(tc.ThinkSplitter(tc._THINK, already_open=True), ["reason</think>\n\nSarah smiled."])
    assert c == "Sarah smiled."


def test_splitter_keeps_newlines_inside_the_reply():
    # Only the start of the content run is trimmed.
    r, c = _run(tc.ThinkSplitter(tc._THINK, already_open=True), ["r", "</think>", "\n\nLine one.\n\nLine two.\n"])
    assert c == "Line one.\n\nLine two.\n"


def test_splitter_trims_pre_state_content_run():
    # Non-thinking output (model never opens a span) still starts clean.
    r, c = _run(tc.ThinkSplitter(tc._GEMMA4), ["\n\n", "Just answering."])
    assert c == "Just answering."


def test_splitter_keeps_the_continuation_space_on_a_prefilled_call():
    # Doc mode's open assistant turn: the leading space joins "He" to "saw".
    r, c = _run(tc.ThinkSplitter(tc._GEMMA4, trim_lead=False), [" saw", " a banana."])
    assert c == " saw a banana."


def test_splitter_retrims_after_a_late_open_tag():
    # Provisional pre-span whitespace is a false start: the trim re-arms so the
    # real reply after the close is still clean.
    r, c = _run(tc.ThinkSplitter(tc._THINK), ["\n", "<think>", "cot", "</think>", "\n\nReply."])
    assert r == "cot"
    assert c == "Reply."


# ── Routed-channel reasoning (Onyx ATEM / Muse Glimmer) ─────────────────────

# Captured from Muse-Glimmer-30B's template and completion stream.
ONYX_TEMPLATE = (
    "{%- if message.get('reasoning_content') -%}"
    "{{- '<|start|>assistant to=self<|message|>' + message['reasoning_content'] + '<|eom|>' -}}"
    "{%- endif -%}"
)
ONYX_LIVE = " to=self<|message|>Name one color.\n\nProbably just Blue.<|start|>assistant to=user<|message|>Blue"


def test_reasoning_format_channel_sniffed_from_the_template():
    fmt = tc.reasoning_format_from_template(ONYX_TEMPLATE)
    assert fmt.channel is True
    assert fmt.open_bytes == " to=self<|message|>"
    assert fmt.disable_bytes == " to=user<|message|>"


def test_reasoning_format_channel_sniffed_from_a_split_header():
    # Templates may assemble headers from separate pieces.
    split = "{{- '<|start|>assistant' + ' to=self' + '<|message|>' + message['reasoning_content'] -}}"
    assert tc.reasoning_format_from_template(split).channel is True


def test_reasoning_format_needs_both_channel_substrings():
    # Either literal alone is not a routing template.
    assert tc.reasoning_format_from_template("mentions to=self in prose").channel is False
    assert tc.reasoning_format_from_template("uses <|message|> only").channel is False


def test_reasoning_format_channel_template_has_no_think_tag():
    assert tc.think_tags_from_template(ONYX_TEMPLATE) == tc._NONE
    assert tc.reasoning_format_from_template(ONYX_TEMPLATE).channel is True


def test_reasoning_format_tag_pair_keeps_its_tags_and_template_toggle():
    fmt = tc.reasoning_format_from_template("...<think>...</think>...")
    assert fmt.channel is False
    assert fmt.tags == tc._THINK
    assert fmt.open_bytes == "<think>"
    # The template controls disable bytes for tag-pair formats.
    assert fmt.disable_bytes == ""


def test_reasoning_format_non_thinking_has_no_bytes():
    fmt = tc.reasoning_format_from_template("plain jinja no markers")
    assert (fmt.channel, fmt.open_bytes, fmt.disable_bytes) == (False, "", "")


def test_channel_splitter_one_token_at_a_time():
    # Every marker arrives split, which is the real streaming case.
    r, c = _run(tc.ChannelSplitter(), list(ONYX_LIVE))
    assert r == "Name one color.\n\nProbably just Blue."
    assert c == "Blue"


def test_channel_splitter_eom_closes_the_thought():
    # The template renders <|eom|> between messages; the model may emit it too.
    r, c = _run(tc.ChannelSplitter(), [" to=self<|message|>cot<|eom|>", "<|start|>assistant to=user<|message|>Hi"])
    assert r == "cot"
    assert c == "Hi"


def test_channel_splitter_reply_without_a_thought_channel():
    # The model may route straight to the user; the header is still scaffolding.
    r, c = _run(tc.ChannelSplitter(), [" to=user<|message|>", "Blue"])
    assert r == ""
    assert c == "Blue"


def test_channel_splitter_header_split_across_chunks():
    r, c = _run(tc.ChannelSplitter(), [" to=", "self<|mess", "age|>", "cot", "<|start|>assist", "ant to=user<|mes", "sage|>Hi"])
    assert r == "cot"
    assert c == "Hi"


def test_channel_splitter_holds_back_a_shared_marker_prefix():
    # Shared marker prefixes must remain buffered across chunks.
    r, c = _run(tc.ChannelSplitter(), [" to=self<|message|>cot<|", "eom|><|start|>assistant to=user<|message|>Hi"])
    assert r == "cot"
    assert c == "Hi"


def test_channel_splitter_drops_a_truncated_header():
    # An incomplete header after a resolved message is scaffolding.
    r, c = _run(tc.ChannelSplitter(), [" to=self<|message|>cot<|start|>assistant to=us"])
    assert r == "cot"
    assert c == ""


def test_channel_splitter_rescues_a_stream_that_never_opens_a_header():
    # Preserve a headerless opening stream instead of losing its reply.
    r, c = _run(tc.ChannelSplitter(), ["Hello there, ", "no routing header at all."])
    assert r == ""
    assert c == "Hello there, no routing header at all."


def test_channel_splitter_rescue_does_not_fire_for_a_primed_start():
    # A primed splitter still drops a later incomplete header.
    r, c = _run(tc.ChannelSplitter(start="content"), ["Hi<|start|>assistant to=us"])
    assert (r, c) == ("", "Hi")


def test_channel_splitter_starts_mid_reply_for_a_prefill():
    # A prefilled reply continues without another header.
    r, c = _run(tc.ChannelSplitter(start="content", trim_lead=False), [" saw", " a banana."])
    assert r == ""
    assert c == " saw a banana."


def test_channel_splitter_starts_mid_thought_for_a_reasoning_prefill():
    r, c = _run(tc.ChannelSplitter(start="reasoning"), [" and so", "<|start|>assistant to=user<|message|>Hi"])
    assert r == " and so"
    assert c == "Hi"


def test_channel_splitter_trims_the_reply_run_only():
    r, c = _run(
        tc.ChannelSplitter(), [" to=self<|message|>cot<|start|>assistant to=user<|message|>\n\nLine one.\n\nLine two.\n"]
    )
    assert c == "Line one.\n\nLine two.\n"


def test_make_splitter_dispatches_on_the_format():
    channel = tc.reasoning_format_from_template(ONYX_TEMPLATE)
    assert isinstance(tc.make_splitter(channel), tc.ChannelSplitter)
    pair = tc.reasoning_format_from_template("<think></think>")
    assert isinstance(tc.make_splitter(pair), tc.ThinkSplitter)


def test_make_splitter_start_reasoning_opens_a_tag_pair_span():
    pair = tc.reasoning_format_from_template("<think></think>")
    r, c = _run(tc.make_splitter(pair, start="reasoning"), ["cot</think>Hi"])
    assert (r, c) == ("cot", "Hi")


def test_opens_reply_channel_only_for_a_bare_channel_call():
    channel = tc.reasoning_format_from_template(ONYX_TEMPLATE)
    pair = tc.reasoning_format_from_template("<think></think>")
    assert tc.opens_reply_channel(channel, reasoning=False, prefill=False) is True
    assert tc.opens_reply_channel(channel, reasoning=True, prefill=False) is False
    # A prefill already owns the prompt tail.
    assert tc.opens_reply_channel(channel, reasoning=False, prefill=True) is False
    assert tc.opens_reply_channel(pair, reasoning=False, prefill=False) is False


# ── Routed-channel reasoning through the transport ──────────────────────────


async def test_complete_text_channel_splits_reasoning_from_the_reply():
    captured: dict = {}
    client = _wired_text_client(
        template="<|start|>assistant",
        props=ONYX_TEMPLATE,
        pieces=[" to=self<|message|>", "Pick a color.", "<|start|>assistant to=user<|message|>", "Blue"],
        captured=captured,
    )
    events = await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m"))
    message = events[-1]["message"]
    assert message["reasoning_content"] == "Pick a color."
    assert message["content"] == "Blue"
    assert [e["delta"] for e in events if e["type"] == "content"] == ["Blue"]
    # Reasoning on: the model writes its own header, so the prompt is untouched.
    assert captured["prompt"] == "<|start|>assistant"


async def test_complete_text_channel_reasoning_off_opens_the_reply_channel():
    captured: dict = {}
    client = _wired_text_client(
        template="<|start|>assistant",
        props=ONYX_TEMPLATE,
        pieces=["Blue"],
        captured=captured,
    )
    events = await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(False)))
    # Channel templates need an explicit reply header when reasoning is off.
    assert captured["prompt"] == "<|start|>assistant to=user<|message|>"
    assert events[-1]["message"]["content"] == "Blue"
    assert "reasoning_content" not in events[-1]["message"]


async def test_complete_text_tag_pair_reasoning_off_leaves_the_prompt_to_the_template():
    captured: dict = {}
    client = _wired_text_client(template="PROMPT", props="<think></think>", pieces=["Blue"], captured=captured)
    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(False)))
    assert captured["prompt"] == "PROMPT"
    assert captured["ctk"] == {"enable_thinking": False, "thinking": False}


async def test_complete_text_channel_forced_call_opens_the_reply_channel():
    # A grammar owns every token, so the model cannot write its own routing
    # header; without one the JSON opens where the template expects that header.
    captured: dict = {}
    tools = [{"type": "function", "function": {"name": "rate", "parameters": {"type": "object"}}}]
    client = _wired_text_client(template="<|start|>assistant", props=ONYX_TEMPLATE, pieces=['{"a":1}'], captured=captured)
    await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "rate"}},
            **reasoning_cfg(True),
        )
    )
    assert captured["prompt"] == "<|start|>assistant to=user<|message|>"


async def test_complete_text_channel_forced_call_yields_to_a_reasoning_prefill():
    # The thought channel is already open and is free text, so the JSON belongs
    # inside it -- exactly as a forced call on a tag-pair model behaves.
    captured: dict = {}
    tools = [{"type": "function", "function": {"name": "rate", "parameters": {"type": "object"}}}]
    client = _wired_text_client(template="<|start|>assistant", props=ONYX_TEMPLATE, pieces=['{"a":1}'], captured=captured)
    await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "rate"}},
            **reasoning_cfg(True, "Weigh it"),
        )
    )
    assert captured["prompt"] == "<|start|>assistant to=self<|message|>Weigh it"


async def test_complete_text_tag_pair_forced_call_leaves_the_prompt_alone():
    # The reply-channel header is a channel-shape byte; a tag-pair template
    # renders its own tail and must not gain one.
    captured: dict = {}
    tools = [{"type": "function", "function": {"name": "rate", "parameters": {"type": "object"}}}]
    client = _wired_text_client(template="PROMPT", props="<think></think>", pieces=['{"a":1}'], captured=captured)
    await _drain(
        client.complete(
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            tools=tools,
            tool_choice={"type": "function", "function": {"name": "rate"}},
            **reasoning_cfg(True),
        )
    )
    assert captured["prompt"] == "PROMPT"


async def test_complete_text_channel_prefill_keeps_the_prompt_tail():
    captured: dict = {}
    client = _wired_text_client(
        template=lambda msgs, ctk: "<|start|>assistant to=user<|message|>He",
        props=ONYX_TEMPLATE,
        pieces=[" saw a banana."],
        captured=captured,
    )
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", prefill="He", **reasoning_cfg(False))
    )
    # The trailing assistant turn already opened the reply channel: no disable
    # bytes on top, and the continuation space survives.
    assert captured["prompt"] == "<|start|>assistant to=user<|message|>He"
    assert events[-1]["message"]["content"] == " saw a banana."


async def test_complete_text_channel_reasoning_prefill_opens_the_thought_channel():
    captured: dict = {}
    client = _wired_text_client(
        template="<|start|>assistant",
        props=ONYX_TEMPLATE,
        pieces=[" so blue.", "<|start|>assistant to=user<|message|>Blue"],
        captured=captured,
    )
    events = await _drain(
        client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(True, prefill="Calm colors"))
    )
    assert captured["prompt"] == "<|start|>assistant to=self<|message|>Calm colors"
    message = events[-1]["message"]
    assert message["reasoning_content"] == "Calm colors so blue."
    assert message["content"] == "Blue"


async def test_complete_text_channel_headerless_stream_still_yields_the_reply():
    # A channel model that skips its routing header must not persist an empty
    # turn; the reply survives even though the split had nothing to key on.
    client = _wired_text_client(
        template="<|start|>assistant",
        props=ONYX_TEMPLATE,
        pieces=["Blue is ", "a fine color."],
    )
    events = await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m"))
    message = events[-1]["message"]
    assert message["content"] == "Blue is a fine color."
    assert "reasoning_content" not in message


async def test_render_prompt_reproduces_the_transports_reply_channel_bytes():
    # The doc-mode auditor re-derives a past generation's prompt to extend it;
    # a byte of drift here is a KV-cache miss on every audited turn.
    captured: dict = {}
    client = _wired_text_client(template="<|start|>assistant", props=ONYX_TEMPLATE, captured=captured)
    await _drain(client.complete(messages=[{"role": "user", "content": "hi"}], model="m", **reasoning_cfg(False)))
    rendered = await client.render_prompt([{"role": "user", "content": "hi"}], reasoning=False)
    assert rendered == captured["prompt"]


async def test_render_prompt_skips_the_sniff_for_a_prefilled_call():
    # A prefill can never need the disable bytes, so it must not pay for /props.
    client = _wired_text_client(template="<|start|>assistant to=user<|message|>He", props=ONYX_TEMPLATE)

    async def must_not_run(root):
        raise AssertionError("/props fetched for a prefilled render")

    client._fetch_chat_template = must_not_run  # type: ignore[method-assign]
    assert await client.render_prompt([{"role": "user", "content": "hi"}], prefill="He") == (
        "<|start|>assistant to=user<|message|>He"
    )
