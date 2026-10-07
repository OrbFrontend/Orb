"""Unit tests for text-completion mode.

The leaf (backend/inference/text_completion.py) is pure, so most tests need no HTTP mocking. Client-level tests supply explicit
reasoning profiles and patch the render/stream seams. Discovery has its own captured-render tests.
"""

import math

import httpx
import pytest

from backend.inference import reasoning_format as rf
from backend.inference import text_completion as tc
from backend.inference.chat_stream import parse_chat_logprobs
from backend.inference.client import LLMClient, parse_tool_calls, reasoning_cfg
from backend.inference.retry import RetryPolicy

GEMMA_OPEN, GEMMA_CLOSE = rf.GEMMA_TAGS
GEMMA_DISABLE = "<|channel>thought\n<channel|>"
HI = [{"role": "user", "content": "hi"}]

# Captured from Muse-Glimmer-30B's template and completion stream.
ONYX_TEMPLATE = (
    "{%- if message.get('reasoning_content') -%}"
    "{{- '<|start|>assistant to=self<|message|>' + message['reasoning_content'] + '<|eom|>' -}}{%- endif -%}"
)
ONYX_LIVE = " to=self<|message|>Name one color.\n\nProbably just Blue.<|start|>assistant to=user<|message|>Blue"
REPLY = "<|start|>assistant to=user<|message|>"


def _run(splitter: tc.Splitter, chunks: list[str]) -> tuple[str, str]:
    """Feed *chunks* + flush; return (reasoning, content) concatenations."""
    reasoning, content = [], []
    for kind, text in [pair for ch in chunks for pair in splitter.feed(ch)] + list(splitter.flush()):
        (reasoning if kind == "reasoning" else content).append(text)
    return "".join(reasoning), "".join(content)


def _think(tags=rf.THINK_TAGS, **kwargs):
    return lambda: tc.ThinkSplitter(tags, **kwargs)


def _channel(**kwargs):
    return lambda: tc.ChannelSplitter(**kwargs)


_G = rf.GEMMA_TAGS
SPLITS = {
    "gemma_open_tag_split_across_three_chunks": (
        _think(_G),
        ["<|channel>", "thought", "\n", "The user", " said hi", "<channel|>", "Hello", "!"],
        ("The user said hi", "Hello!"),
    ),
    "gemma_close_tag_split_across_chunks": (_think(_G), [GEMMA_OPEN, "abc", "<channel", "|>Hi"], ("abc", "Hi")),
    "think_pair": (_think(), ["<think>", "reason", "</think>", "answer"], ("reason", "answer")),
    "already_open_starts_in_reasoning": (_think(already_open=True), ["reason", "</think>", "answer"], ("reason", "answer")),
    "non_thinking_passthrough": (_think(rf.NO_TAGS), ["hello ", "world"], ("", "hello world")),
    "reasoning_on_but_no_channel_is_all_content": (_think(_G), ["Just ", "answering."], ("", "Just answering.")),
    "flush_drains_mid_reasoning_tail_as_reasoning": (_think(_G), [GEMMA_OPEN, "text", "<chan"], ("text<chan", "")),
    "flush_drains_pre_state_tail_as_content": (_think(_G), ["<|chan"], ("", "<|chan")),
    "minimax_pair": (_think(rf.MINIMAX_TAGS), ["<mm:think>", "reason", "</mm:think>", "answer"], ("reason", "answer")),
    "trims_template_padding_after_close_tag": (
        _think(already_open=True),
        ["reason", "</think>", "\n\n", "Sarah smiled."],
        ("reason", "Sarah smiled."),
    ),
    "trims_when_padding_shares_the_close_chunk": (
        _think(already_open=True),
        ["reason</think>\n\nSarah smiled."],
        ("reason", "Sarah smiled."),
    ),
    "keeps_newlines_inside_the_reply": (
        _think(already_open=True),
        ["r", "</think>", "\n\nLine one.\n\nLine two.\n"],
        ("r", "Line one.\n\nLine two.\n"),
    ),
    "trims_pre_state_content_run": (_think(_G), ["\n\n", "Just answering."], ("", "Just answering.")),
    "keeps_the_continuation_space_on_a_prefilled_call": (
        _think(_G, trim_lead=False),
        [" saw", " a banana."],
        ("", " saw a banana."),
    ),
    "retrims_after_a_late_open_tag": (_think(), ["\n", "<think>", "cot", "</think>", "\n\nReply."], ("cot", "Reply.")),
    "channel_one_token_at_a_time": (_channel(), list(ONYX_LIVE), ("Name one color.\n\nProbably just Blue.", "Blue")),
    "channel_eom_closes_the_thought": (_channel(), [" to=self<|message|>cot<|eom|>", REPLY + "Hi"], ("cot", "Hi")),
    "channel_reply_without_a_thought_channel": (_channel(), [" to=user<|message|>", "Blue"], ("", "Blue")),
    "channel_header_split_across_chunks": (
        _channel(),
        [" to=", "self<|mess", "age|>", "cot", "<|start|>assist", "ant to=user<|mes", "sage|>Hi"],
        ("cot", "Hi"),
    ),
    "channel_holds_back_a_shared_marker_prefix": (
        _channel(),
        [" to=self<|message|>cot<|", "eom|>" + REPLY + "Hi"],
        ("cot", "Hi"),
    ),
    "channel_drops_a_truncated_header": (_channel(), [" to=self<|message|>cot<|start|>assistant to=us"], ("cot", "")),
    "channel_rescues_a_stream_that_never_opens_a_header": (
        _channel(),
        ["Hello there, ", "no routing header at all."],
        ("", "Hello there, no routing header at all."),
    ),
    "channel_rescue_does_not_fire_for_a_primed_start": (
        _channel(start="content"),
        ["Hi<|start|>assistant to=us"],
        ("", "Hi"),
    ),
    "channel_starts_mid_reply_for_a_prefill": (
        _channel(start="content", trim_lead=False),
        [" saw", " a banana."],
        ("", " saw a banana."),
    ),
    "channel_starts_mid_thought_for_a_reasoning_prefill": (
        _channel(start="reasoning"),
        [" and so", REPLY + "Hi"],
        (" and so", "Hi"),
    ),
    "channel_trims_the_reply_run_only": (
        _channel(),
        [" to=self<|message|>cot" + REPLY + "\n\nLine one.\n\nLine two.\n"],
        ("cot", "Line one.\n\nLine two.\n"),
    ),
}


@pytest.mark.parametrize("make,chunks,expected", SPLITS.values(), ids=SPLITS.keys())
def test_splitter(make, chunks, expected):
    assert _run(make(), chunks) == expected


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


def test_build_completion_params_n_probs():
    out = tc.build_completion_params({"n_probs": 10})
    assert out["n_probs"] == 10 and out["post_sampling_probs"] is True
    # Absent, 0, negatives, and the bool True (an int subclass) are not real requests.
    for params in ({"temperature": 0.7}, *({"n_probs": bad} for bad in (0, -1, True, "10", None))):
        out = tc.build_completion_params(params)
        assert "n_probs" not in out and "post_sampling_probs" not in out, params


def _approx_exp(x):
    return pytest.approx(math.exp(x))


@pytest.mark.parametrize(
    "data,expected",
    [
        (
            [
                {
                    "token": " Paris",
                    "prob": 0.86,
                    "top_probs": [{"token": " Paris", "prob": 0.86}, {"token": " own", "prob": 0.1}],
                }
            ],
            [{"token": " Paris", "prob": 0.86, "top": [{"t": " Paris", "p": 0.86}, {"t": " own", "p": 0.1}]}],
        ),
        (
            [
                {
                    "token": "x",
                    "logprob": -0.1,
                    "top_logprobs": [{"token": "x", "logprob": -0.1}, {"token": "y", "logprob": -2.0}],
                }
            ],
            [
                {
                    "token": "x",
                    "prob": _approx_exp(-0.1),
                    "top": [{"t": "x", "p": _approx_exp(-0.1)}, {"t": "y", "p": _approx_exp(-2.0)}],
                }
            ],
        ),
        # Legacy shape has no top-level prob; the sampled token's prob is read from the alternatives.
        (
            [{"content": " the", "probs": [{"tok_str": " the", "prob": 0.7}, {"tok_str": " a", "prob": 0.2}]}],
            [{"token": " the", "prob": 0.7, "top": [{"t": " the", "p": 0.7}, {"t": " a", "p": 0.2}]}],
        ),
        (
            [{"token": "a", "prob": 0.9, "top_probs": []}, {"token": "b", "prob": 0.5}],
            [{"token": "a", "prob": 0.9, "top": []}, {"token": "b", "prob": 0.5, "top": []}],
        ),
        ("absent", []),
        (None, []),
        ("nope", []),
        ([42, "x", None], []),
        ([{"prob": 0.5}], []),
    ],
)
def test_parse_token_probs(data, expected):
    assert tc.parse_token_probs({} if data == "absent" else {"completion_probabilities": data}) == expected


@pytest.mark.parametrize(
    "choice,expected",
    [
        (
            {
                "logprobs": {
                    "content": [
                        {
                            "token": " the",
                            "logprob": -0.2,
                            "top_logprobs": [{"token": " the", "logprob": -0.2}, {"token": " a", "logprob": -1.6}],
                        }
                    ]
                }
            },
            [
                {
                    "token": " the",
                    "prob": _approx_exp(-0.2),
                    "top": [{"t": " the", "p": _approx_exp(-0.2)}, {"t": " a", "p": _approx_exp(-1.6)}],
                }
            ],
        ),
        ({}, []),
        ({"logprobs": None}, []),
        ({"logprobs": {}}, []),
        ({"logprobs": {"content": None}}, []),
        (
            {
                "logprobs": {
                    "content": [
                        42,
                        {"token": None, "logprob": -0.1},  # non-str token -> skip
                        {"token": "x"},  # no readable prob -> degrades to 0.0
                        {"token": "y", "logprob": -0.5, "top_logprobs": ["junk", {"token": "z", "logprob": -0.9}]},
                    ]
                }
            },
            [
                {"token": "x", "prob": 0.0, "top": []},
                {"token": "y", "prob": _approx_exp(-0.5), "top": [{"t": "z", "p": _approx_exp(-0.9)}]},
            ],
        ),
    ],
)
def test_parse_chat_logprobs(choice, expected):
    assert parse_chat_logprobs(choice) == expected


def test_synthesize_usage():
    usage = tc.synthesize_usage({"tokens_evaluated": 46, "tokens_predicted": 12, "timings": {"prompt_n": 5}})
    assert (usage["prompt_tokens"], usage["completion_tokens"], usage["total_tokens"]) == (46, 12, 58)
    assert usage["prompt_tokens_details"]["cached_tokens"] == 41  # 46 - 5
    usage = tc.synthesize_usage({"tokens_evaluated": 3, "tokens_predicted": 1, "timings": {"prompt_n": 9}})
    assert usage["prompt_tokens_details"]["cached_tokens"] == 0


def test_forced_tool_message_survives_parse_tool_calls():
    msg = tc.forced_tool_message("rate", '{"mood":"happy","score":3}')
    assert msg["content"] == ""
    assert parse_tool_calls(msg) == [{"name": "rate", "arguments": {"mood": "happy", "score": 3}}]


def test_forced_schema():
    tools = [
        {"type": "function", "function": {"name": "a", "parameters": {"type": "object", "x": 1}}},
        {"type": "function", "function": {"name": "b", "parameters": {"type": "object", "y": 2}}},
    ]
    assert tc.forced_schema(tools, {"type": "function", "function": {"name": "b"}}) == {"type": "object", "y": 2}
    for choice in ("auto", "required", None):
        assert tc.forced_schema(tools, choice) is None
    assert tc.forced_schema(None, {"type": "function", "function": {"name": "a"}}) is None


def test_has_image_parts():
    assert tc.has_image_parts([{"role": "user", "content": [{"type": "image_url", "image_url": {}}]}])
    assert not tc.has_image_parts([{"role": "user", "content": "plain text"}])
    assert not tc.has_image_parts([{"role": "user", "content": [{"type": "text", "text": "hi"}]}])


def test_reasoning_enabled_reads_reasoning_cfg():
    assert tc.reasoning_enabled(reasoning_cfg(True)) is True
    assert tc.reasoning_enabled(reasoning_cfg(False)) is False
    assert tc.reasoning_enabled({}) is True  # default on


def test_reasoning_cfg_carries_prefill_only_when_reasoning_on():
    assert reasoning_cfg(True, "seed")["reasoning_prefill"] == "seed"
    assert "reasoning_prefill" not in reasoning_cfg(True)
    assert "reasoning_prefill" not in reasoning_cfg(True, "")
    assert "reasoning_prefill" not in reasoning_cfg(False, "seed")


# -- Client-level wiring (patched HTTP seams) ----------------------------------


def _format(props: str) -> rf.ReasoningFormat:
    """Explicit protocol fixtures for transport tests; discovery is tested separately."""
    if "to=self" in props:
        return rf.ReasoningFormat(
            status="known",
            channel=True,
            controls=rf.ReasoningControls(" to=self<|message|>", " to=user<|message|>", "<|eom|>" + REPLY),
        )
    if "<|channel>thought" in props:
        return rf.ReasoningFormat(
            status="known", tags=rf.GEMMA_TAGS, controls=rf.ReasoningControls(GEMMA_OPEN, GEMMA_DISABLE, GEMMA_CLOSE)
        )
    if "<think>" in props:
        return rf.ReasoningFormat(
            status="known",
            tags=rf.THINK_TAGS,
            controls=rf.ReasoningControls("<think>\n", "<think>\n\n</think>\n\n", "\n</think>\n\n"),
        )
    return rf.ReasoningFormat()


def test_make_splitter_dispatches_on_the_format():
    assert isinstance(tc.make_splitter(_format(ONYX_TEMPLATE)), tc.ChannelSplitter)
    pair = _format("<think></think>")
    assert isinstance(tc.make_splitter(pair), tc.ThinkSplitter)
    assert _run(tc.make_splitter(pair, start="reasoning"), ["cot</think>Hi"]) == ("cot", "Hi")


async def _drain(agen):
    return [e async for e in agen]


async def _complete(client: LLMClient, **kwargs) -> list[dict]:
    return await _drain(client.complete(messages=HI, model="m", **kwargs))


def _joined(events: list[dict], kind: str) -> str:
    return "".join(e["delta"] for e in events if e.get("type") == kind)


def _forced(name="rate", parameters=None) -> dict:
    return {
        "tools": [{"type": "function", "function": {"name": name, "parameters": parameters or {"type": "object"}}}],
        "tool_choice": {"type": "function", "function": {"name": name}},
    }


def _wired(template="P", props="", pieces=("x",), final=None) -> tuple[LLMClient, dict]:
    """Text client with deterministic /apply-template, /props and stream seams, plus what they captured."""
    client = LLMClient("http://x/v1", completion_mode="text")
    captured: dict = {}

    async def fake_apply(root, msgs, chat_template_kwargs=None):
        captured["msgs"] = list(msgs)
        captured["ctk"] = chat_template_kwargs
        return template(msgs, chat_template_kwargs) if callable(template) else template

    async def fake_format(root):
        return _format(props)

    async def fake_stream(url, body):
        captured["body"] = body
        captured["prompt"] = body["prompt"]
        for piece in pieces:
            yield {"content": piece, "stop": False}
        yield final or {"content": "", "stop": True, "tokens_evaluated": 1, "tokens_predicted": 1, "timings": {"prompt_n": 1}}

    client._apply_template = fake_apply  # type: ignore[method-assign]
    client._reasoning_format = fake_format  # type: ignore[method-assign]
    client._stream_completion = fake_stream  # type: ignore[method-assign]
    return client, captured


def _chat_client(captured: dict, content=None) -> LLMClient:
    client = LLMClient("http://x/v1", completion_mode="chat")

    async def fake_chat(messages, model, tools=None, tool_choice=None, **params):
        captured.update(tools=tools, tool_choice=tool_choice, params=params)
        yield {"type": "done", "message": {"content": content} if content else {}, "usage": None}

    client._complete_chat = fake_chat  # type: ignore[method-assign]
    return client


async def test_complete_text_forced_call_end_to_end():
    final = {"content": "", "stop": True, "tokens_evaluated": 10, "tokens_predicted": 5, "timings": {"prompt_n": 4}}
    client, _ = _wired("PROMPT", "<|channel>thought", ['{"mood"', ':"happy"', ',"score":1}'], final)
    events = await _complete(client, **_forced())
    assert not any(e["type"] == "content" for e in events)  # forced -> no content deltas
    done = events[-1]
    assert parse_tool_calls(done["message"]) == [{"name": "rate", "arguments": {"mood": "happy", "score": 1}}]
    assert done["usage"]["prompt_tokens"] == 10
    assert done["usage"]["prompt_tokens_details"]["cached_tokens"] == 6


async def test_complete_text_enable_thinking_delegated_to_template_no_manual_suffix():
    # The template owns reasoning on/off: the client forwards enable_thinking to /apply-template and does NOT hand-append
    # disable bytes (which double-opened Qwen3's pre-opened <think>). The fake template echoes what it was told.
    client, captured = _wired(
        lambda _msgs, ctk: "BASE" + ("" if (ctk or {}).get("enable_thinking", True) else GEMMA_DISABLE),
        "<|channel>thought",
        ["hi"],
    )
    await _complete(client, **reasoning_cfg(False))
    assert captured["ctk"] == {"enable_thinking": False, "thinking": False}  # both toggle aliases
    assert captured["prompt"] == "BASE" + GEMMA_DISABLE  # from the template, not the client
    await _complete(client, **reasoning_cfg(True))
    assert captured["ctk"] == {"enable_thinking": True, "thinking": True}
    assert captured["prompt"] == "BASE"


@pytest.mark.parametrize(
    "template,on",
    [
        # Qwen3: the template pre-opens <think>, so the model stream starts INSIDE reasoning (no leading <think>).
        ("<|im_start|>assistant\n<think>\n", True),
        # Kimi K2: the template keys thinking off `thinking`, so a reasoning-OFF request still renders a pre-opened <think>;
        # the pre-open is detected from the rendered bytes, not our flag.
        ("<|im_assistant|>assistant<|im_middle|><think>", False),
    ],
)
async def test_complete_text_primes_splitter_when_prompt_pre_opens_think(template, on):
    client, _ = _wired(template, "<think>...</think>", ["Analyzing", " the ask.", "</think>", "\n\nSarah smiled."])
    events = await _complete(client, **reasoning_cfg(on))
    assert _joined(events, "reasoning") == "Analyzing the ask."
    assert _joined(events, "content") == "Sarah smiled."  # padding trimmed, no leaked special token


async def test_lane_thinking_keeps_the_thinking_on_prefix_and_closes_the_thought_at_the_tail():
    # Qwen3.8 and Gemma 4 rewrite the top of the system turn on enable_thinking; a reasoning-off call on a lane where another
    # pass reasons keeps the thinking-on bytes and closes the span at the tail, so both share one prefix.
    def template(_msgs, ctk):
        return ("EFFORT\nSYS" if (ctk or {}).get("enable_thinking") else "SYS") + "<|im_start|>assistant\n<think>\n"

    client, captured = _wired(template, "<think>...</think>", ["Sarah smiled."])
    await _complete(client, **reasoning_cfg(True))
    reasoning_prompt = captured["prompt"]
    events = await _complete(client, template_thinking=True, **reasoning_cfg(False))
    assert captured["ctk"]["enable_thinking"] is True
    assert captured["prompt"] == "EFFORT\nSYS<|im_start|>assistant\n<think>\n\n</think>\n\n"
    assert captured["prompt"].startswith(reasoning_prompt.removesuffix("<think>\n"))
    assert _joined(events, "content") == "Sarah smiled."
    assert _joined(events, "reasoning") == ""


async def test_complete_text_prefill_appends_assistant_message():
    client, captured = _wired()
    await _complete(client, prefill="Once upon")
    assert captured["msgs"][-1] == {"role": "assistant", "content": "Once upon"}
    assert captured["ctk"] is None  # prefill skips enable_thinking; the trailing turn governs it


async def test_complete_text_forced_prefill_prepends_arguments():
    # Editor prefill path: arguments = prompt-side prefill bytes + generated remainder, so json.loads sees one complete object.
    client, _ = _wired(pieces=['REPL"', "}]}"])
    events = await _complete(client, **_forced("editor_apply_patch"), prefill='{"patches": [{"search": "old", "replace": "')
    assert parse_tool_calls(events[-1]["message"]) == [
        {"name": "editor_apply_patch", "arguments": {"patches": [{"search": "old", "replace": "REPL"}]}}
    ]


async def test_complete_text_grammar_overrides_json_schema():
    client, captured = _wired()
    await _complete(client, **_forced("t"), grammar='root ::= "x"')
    assert captured["body"]["grammar"] == 'root ::= "x"'
    assert "json_schema" not in captured["body"]


async def test_complete_text_json_schema_narrows_forced_grammar():
    # Per-fragment director steps: the caller-supplied json_schema replaces the tool-derived one.
    client, captured = _wired(pieces=["{}"])
    full = {"type": "object", "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    narrow = {"type": "object", "properties": {"a": {"type": "string"}}, "required": []}
    await _complete(client, **_forced("t", full), json_schema=narrow)
    assert captured["body"]["json_schema"] == narrow


async def test_chat_transport_drops_grammar_and_prefill():
    captured: dict = {}
    await _complete(_chat_client(captured), grammar="root ::= x", json_schema={"type": "object"}, prefill="X")
    assert "grammar" not in captured["params"] and "prefill" not in captured["params"]
    # json_schema is passed through: the chat transport consumes it for structured forced calls.
    assert captured["params"]["json_schema"] == {"type": "object"}


async def test_complete_text_apply_template_error_falls_back_to_chat():
    captured: dict = {}
    client, _ = _wired()

    async def boom(root, msgs, chat_template_kwargs=None):
        raise httpx.ConnectError("nope")

    client._apply_template = boom  # type: ignore[method-assign]
    client._complete_chat = _chat_client(captured, "CHAT")._complete_chat  # type: ignore[method-assign]
    forced = _forced()
    assert (await _complete(client, **forced))[-1]["message"]["content"] == "CHAT"
    assert captured["tools"] == forced["tools"]  # still sources the response_format schema
    assert captured["tool_choice"] == forced["tool_choice"]
    assert captured["params"]["tools_in_prompt"] is False


async def test_image_call_routes_through_chat_transport():
    client, _ = _wired()

    async def must_not_run(*a, **k):
        raise AssertionError("text transport used for an image-bearing call")
        yield  # pragma: no cover -- makes this an async generator

    client._complete_chat = _chat_client({}, "CHAT")._complete_chat  # type: ignore[method-assign]
    client._complete_text = must_not_run  # type: ignore[method-assign]
    msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}]
    assert (await _drain(client.complete(messages=msgs, model="m")))[-1]["message"]["content"] == "CHAT"


# -- Reasoning prefill ---------------------------------------------------------


async def test_reasoning_prefill_appends_to_pre_opened_think():
    # Qwen3 shape: the template already opened <think>, so only the seed text is appended (no second open tag), and it is echoed
    # as reasoning ahead of the model's own deltas.
    client, captured = _wired("<|im_start|>assistant\n<think>\n", "<think>...</think>", ["CoT", "</think>", "text"])
    events = await _complete(client, **reasoning_cfg(True, "I will think. "))
    assert captured["prompt"] == "<|im_start|>assistant\n<think>\nI will think. "
    assert events[0] == {"type": "reasoning", "delta": "I will think. "}
    assert _joined(events, "reasoning") == "I will think. CoT"  # echo + the model's continuation
    assert _joined(events, "content") == "text"


async def test_reasoning_prefill_opens_think_when_template_does_not():
    # Gemma 4 shape: the template leaves the open tag to the model, so Orb injects it.
    client, captured = _wired("BASE", "<|channel>thought")
    await _complete(client, **reasoning_cfg(True, "Seed."))
    assert captured["prompt"] == "BASE" + GEMMA_OPEN + "Seed."


@pytest.mark.parametrize(
    "props,kwargs",
    [
        ("<|channel>thought", reasoning_cfg(False, "Seed.")),
        ("", reasoning_cfg(True, "Seed.")),  # non-thinking template
        # The assistant prefill already owns the prompt tail; the two cannot both.
        ("<|channel>thought", {"prefill": "Once upon", **reasoning_cfg(True, "Seed.")}),
    ],
)
async def test_reasoning_prefill_ignored(props, kwargs):
    client, captured = _wired("BASE", props)
    events = await _complete(client, **kwargs)
    assert captured["prompt"] == "BASE"
    assert not any(e["type"] == "reasoning" for e in events)


async def test_reasoning_prefill_preserves_retry_window():
    # The echo is emitted on the first streamed chunk, not before the POST, so a transient failure is still a clean retry.
    client, _ = _wired("BASE", "<|channel>thought")
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
    events = await _complete(client, **reasoning_cfg(True, "Seed."))
    assert attempts["n"] == 2  # retried
    assert events[0] == {"type": "reasoning", "delta": "Seed."}
    assert sum(1 for e in events if e.get("delta") == "Seed.") == 1  # echoed exactly once


# -- Routed-channel reasoning through the transport --------------------------


async def test_complete_text_channel_splits_reasoning_from_the_reply():
    client, captured = _wired("<|start|>assistant", ONYX_TEMPLATE, [" to=self<|message|>", "Pick a color.", REPLY, "Blue"])
    events = await _complete(client)
    message = events[-1]["message"]
    assert message["reasoning_content"] == "Pick a color."
    assert message["content"] == "Blue"
    assert [e["delta"] for e in events if e["type"] == "content"] == ["Blue"]
    # Reasoning on: the model writes its own header, so the prompt is untouched.
    assert captured["prompt"] == "<|start|>assistant"


async def test_complete_text_channel_reasoning_off_opens_the_reply_channel():
    client, captured = _wired("<|start|>assistant", ONYX_TEMPLATE, ["Blue"])
    events = await _complete(client, **reasoning_cfg(False))
    # Channel templates need an explicit reply header when reasoning is off.
    assert captured["prompt"] == REPLY
    assert events[-1]["message"]["content"] == "Blue"
    assert "reasoning_content" not in events[-1]["message"]


async def test_complete_text_tag_pair_reasoning_off_leaves_the_prompt_to_the_template():
    client, captured = _wired("PROMPT", "<think></think>", ["Blue"])
    await _complete(client, **reasoning_cfg(False))
    assert captured["prompt"] == "PROMPT"
    assert captured["ctk"] == {"enable_thinking": False, "thinking": False}


FORCED_TAILS = {
    # A grammar owns every token, so a channel model cannot write its own routing header.
    "channel_opens_the_reply_channel": (ONYX_TEMPLATE, "<|start|>assistant", "", REPLY),
    # The thought channel is already open and free text, so the JSON belongs inside it.
    "channel_yields_to_a_reasoning_prefill": (
        ONYX_TEMPLATE,
        "<|start|>assistant",
        "Weigh it",
        "<|start|>assistant to=self<|message|>Weigh it",
    ),
    # A tag-pair model never gains a channel header; it closes its span with the template's own disable pair.
    "tag_pair_never_gains_a_channel_header": ("<think></think>", "PROMPT", "", "PROMPT<think>\n\n</think>\n\n"),
    # Measured on Gemma 4 31B: an open thought span under a grammar degenerates into one repeated word.
    "tag_pair_closes_the_thought_span": ("<|channel>thought", "PROMPT", "", "PROMPT" + GEMMA_DISABLE),
    # Qwen3.8 opens <think> itself; the rewrite lands on the template's own reasoning-off tail.
    "closes_a_pre_opened_span": ("<think>...</think>", "PROMPT<think>\n", "", "PROMPT<think>\n\n</think>\n\n"),
    "non_thinking_appends_nothing": ("plain jinja no markers", "PROMPT", "", "PROMPT"),
}


@pytest.mark.parametrize("props,template,seed,prompt", FORCED_TAILS.values(), ids=FORCED_TAILS.keys())
async def test_complete_text_forced_call_prompt_tail(props, template, seed, prompt):
    client, captured = _wired(template, props, ['{"a":1}'])
    await _complete(client, **_forced(), **reasoning_cfg(True, seed))
    assert captured["prompt"] == prompt


async def test_complete_text_channel_prefill_keeps_the_prompt_tail():
    client, captured = _wired(lambda msgs, ctk: REPLY + "He", ONYX_TEMPLATE, [" saw a banana."])
    events = await _complete(client, prefill="He", **reasoning_cfg(False))
    # The trailing assistant turn already opened the reply channel: no disable bytes, and the continuation space survives.
    assert captured["prompt"] == REPLY + "He"
    assert events[-1]["message"]["content"] == " saw a banana."


async def test_complete_text_channel_reasoning_prefill_opens_the_thought_channel():
    client, captured = _wired("<|start|>assistant", ONYX_TEMPLATE, [" so blue.", REPLY + "Blue"])
    events = await _complete(client, **reasoning_cfg(True, prefill="Calm colors"))
    assert captured["prompt"] == "<|start|>assistant to=self<|message|>Calm colors"
    assert events[-1]["message"]["reasoning_content"] == "Calm colors so blue."
    assert events[-1]["message"]["content"] == "Blue"


async def test_complete_text_channel_headerless_stream_still_yields_the_reply():
    # A channel model that skips its routing header must not persist an empty turn.
    client, _ = _wired("<|start|>assistant", ONYX_TEMPLATE, ["Blue is ", "a fine color."])
    message = (await _complete(client))[-1]["message"]
    assert message["content"] == "Blue is a fine color."
    assert "reasoning_content" not in message


async def test_render_prompt_reproduces_the_transports_reply_channel_bytes():
    # The doc-mode auditor re-derives a past generation's prompt; a byte of drift is a KV-cache miss on every audited turn.
    client, captured = _wired("<|start|>assistant", ONYX_TEMPLATE)
    await _complete(client, **reasoning_cfg(False))
    assert await client.render_prompt(HI, reasoning=False) == captured["prompt"]


async def test_render_prompt_skips_the_sniff_for_a_prefilled_call():
    # A prefill can never need the disable bytes, so it must not pay for /props.
    client, _ = _wired(REPLY + "He", ONYX_TEMPLATE)

    async def must_not_run(root):
        raise AssertionError("/props fetched for a prefilled render")

    client._reasoning_format = must_not_run  # type: ignore[method-assign]
    assert await client.render_prompt(HI, prefill="He") == REPLY + "He"


# -- Reasoning effort rides the text-mode render -----------------------------


def _effort_client(effort: str, refuse: tuple[str, ...] = ()) -> tuple[LLMClient, dict]:
    """Text client whose /apply-template refuses the levels in *refuse*."""
    client, captured = _wired("P", "<think></think>")
    client.reasoning_effort = effort
    real = client._apply_template

    async def apply(root, msgs, chat_template_kwargs=None):
        captured.setdefault("attempts", []).append(chat_template_kwargs)
        if chat_template_kwargs and chat_template_kwargs.get("reasoning_effort") in refuse:
            raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500))
        return await real(root, msgs, chat_template_kwargs)

    client._apply_template = apply  # type: ignore[method-assign]
    return client, captured


@pytest.mark.parametrize(
    "effort,reasoning,ctk",
    [
        ("low", True, {"enable_thinking": True, "thinking": True, "reasoning_effort": "low"}),
        ("low", False, {"enable_thinking": False, "thinking": False}),
        # 'custom' names a request-body field, which a chat template cannot read.
        ("custom", True, {"enable_thinking": True, "thinking": True}),
    ],
)
async def test_render_prompt_reasoning_effort(effort, reasoning, ctk):
    client, captured = _effort_client(effort)
    client.reasoning_effort_param, client.reasoning_effort_value = "think_budget", "512"
    await client.render_prompt(HI, reasoning=reasoning)
    assert captured["ctk"] == ctk


async def test_render_prompt_retries_without_a_refused_reasoning_effort():
    # Qwen3.8 accepts only low/medium/xhigh and raises on the rest; dropping the effort keeps the call in the text transport.
    client, captured = _effort_client("high", refuse=("high",))
    assert await client.render_prompt(HI, reasoning=True) == "P"
    assert captured["attempts"] == [
        {"enable_thinking": True, "thinking": True, "reasoning_effort": "high"},
        {"enable_thinking": True, "thinking": True},
    ]
    # End-to-end: a refused level must not drop the turn to the chat transport.
    client, captured = _effort_client("high", refuse=("high",))
    assert (await _complete(client, **reasoning_cfg(True)))[-1]["message"]["content"] == "x"
    assert captured["prompt"] == "P"


async def test_render_prompt_reraises_a_failure_that_is_not_about_effort():
    # No effort to drop -> the caller's chat-transport fallback still owns this.
    client, _ = _effort_client("")

    async def always_fail(root, msgs, chat_template_kwargs=None):
        raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=httpx.Response(500))

    client._apply_template = always_fail  # type: ignore[method-assign]
    with pytest.raises(httpx.HTTPStatusError):
        await client.render_prompt(HI, reasoning=True)
