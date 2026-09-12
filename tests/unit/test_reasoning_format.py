"""Captured llama.cpp renders plus regressions for discovery and prompt state."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from backend.inference import reasoning_format as rf
from backend.inference import text_completion as tc
from backend.inference.client import LLMClient, reasoning_cfg
from backend.inference.retry import RetryPolicy

FIXTURES = Path(__file__).parents[1] / "fixtures" / "reasoning"


def _case(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _renders(name):
    return {key: value["prompt"] for key, value in _case(name)["renders"].items()}


@pytest.mark.parametrize(
    "name,tags,channel,reply",
    [
        ("qwen", ("<think>", "</think>"), False, "<think>\n\n</think>\n\n"),
        (
            "gemma",
            ("<|channel>thought\n", "<channel|>"),
            False,
            "<|channel>thought\n<channel|>",
        ),
        ("muse", ("", ""), True, " to=user<|message|>"),
    ],
)
def test_captured_protocols(name, tags, channel, reply):
    fmt = rf.format_from_probes(**_renders(name))
    assert fmt.status == "known"
    assert fmt.tags == tags
    assert fmt.channel == channel
    assert fmt.controls is not None
    assert fmt.controls.reply == reply


@pytest.mark.parametrize("name", ["qwen", "gemma", "muse"])
@pytest.mark.parametrize("width", [1, 2, 7, 1000])
def test_captured_boundaries_survive_stream_chunking(name, width):
    fmt = rf.format_from_probes(**_renders(name))
    assert fmt.controls
    raw = fmt.controls.open + "Consider blue." + fmt.controls.close + "Blue."
    splitter = tc.make_splitter(fmt)
    pieces = []
    for i in range(0, len(raw), width):
        pieces.extend(splitter.feed(raw[i : i + width]))
    pieces.extend(splitter.flush())
    assert "".join(text for kind, text in pieces if kind == "reasoning").strip() == "Consider blue."
    assert "".join(text for kind, text in pieces if kind == "content") == "Blue."


@pytest.mark.parametrize("name", ["qwen", "gemma", "muse"])
@pytest.mark.parametrize("scenario", ["on", "off", "seed", "forced", "forced_off"])
@pytest.mark.parametrize("rechunk", [False, True])
def test_recorded_live_completions(name, scenario, rechunk):
    recording = next(row for row in _case(name)["streams"] if row["case"] == scenario)
    fmt = rf.format_from_probes(**_renders(name))
    splitter = tc.make_splitter(fmt, start=recording["start"])
    chunks = list("".join(recording["chunks"])) if rechunk else recording["chunks"]
    pieces = [piece for chunk in chunks for piece in splitter.feed(chunk)] + splitter.flush()
    assert "".join(text for kind, text in pieces if kind == "reasoning") == recording["reasoning"]
    assert "".join(text for kind, text in pieces if kind == "content") == recording["content"]
    if scenario.startswith("forced"):
        assert isinstance(json.loads(recording["content"])["color"], str)


@pytest.mark.parametrize("name", ["qwen", "gemma", "muse"])
def test_constrained_prompt_preserves_prefix_and_is_idempotent(name):
    renders = _renders(name)
    fmt = rf.format_from_probes(**renders)
    assert fmt.controls
    on = renders["on"]
    prepared = rf.prepare_prompt(on, fmt, reasoning=True, constrained=True)
    base = on[: -len(fmt.controls.open)] if on.endswith(fmt.controls.open) else on
    assert prepared.text == base + fmt.controls.reply
    assert prepared.start == "content"
    assert rf.prepare_prompt(prepared.text, fmt, reasoning=True, constrained=True) == prepared


@pytest.mark.parametrize("name", ["qwen", "gemma", "muse"])
def test_constrained_call_closes_observed_reasoning_even_when_requested_off(name):
    fmt = rf.format_from_probes(**_renders(name))
    assert fmt.controls
    prompt = "STABLE PREFIX" + fmt.controls.open
    prepared = rf.prepare_prompt(prompt, fmt, reasoning=False, constrained=True)
    assert prepared.start == "content"
    assert prepared.text.startswith("STABLE PREFIX")
    assert rf.prompt_state(prepared.text, fmt) == "content"


@pytest.mark.parametrize("name", ["qwen", "gemma", "muse"])
def test_reasoning_prefill_uses_observed_open_state(name):
    renders = _renders(name)
    fmt = rf.format_from_probes(**renders)
    assert fmt.controls
    prepared = rf.prepare_prompt(renders["on"], fmt, reasoning=True, reasoning_prefill="Consider ")
    assert prepared.start == "reasoning"
    assert prepared.reasoning_prefill == "Consider "
    assert prepared.text.endswith(fmt.controls.open + "Consider ")
    assert prepared.text.count(fmt.controls.open) == 1


def test_explicit_content_start_preserves_literal_tags_and_whitespace():
    fmt = rf.format_from_probes(**_renders("qwen"))
    splitter = tc.make_splitter(fmt, start="content", trim_lead=False)
    text = " The example is <think>hello</think>."
    assert splitter.feed(text) + splitter.flush() == [("content", text)]


def test_assistant_prefill_owns_state_even_if_it_ends_with_a_reasoning_tag():
    fmt = rf.format_from_probes(**_renders("qwen"))
    prepared = rf.prepare_prompt(
        "An XML example: <think>",
        fmt,
        reasoning=True,
        prefill=True,
        reasoning_prefill="seed",
    )
    assert prepared == rf.PreparedPrompt("An XML example: <think>", "content")


def test_closed_render_blocks_conflicting_reasoning_prefill():
    renders = _renders("qwen")
    fmt = rf.format_from_probes(**renders)
    with pytest.raises(rf.ReasoningFormatError, match="already selects a reply"):
        rf.prepare_prompt(renders["off"], fmt, reasoning=True, reasoning_prefill="seed")


@pytest.mark.parametrize(
    "tag",
    [
        "think",
        "mm:think",
        "seed:think",
        "think:opensource",
        "thinking",
        "thought",
        "reasoning",
    ],
)
def test_rendered_tag_pair_needs_no_jinja_expression_interpreter(tag):
    history = f"Ignored example: <think>. ASSISTANT<{tag}> {rf.PROBE_REASONING} </{tag}>\n{rf.PROBE_CONTENT}"
    fmt = rf.format_from_probes(
        on="ASSISTANT",
        off="ASSISTANT",
        history=history,
        empty_history=history.replace(rf.PROBE_REASONING, ""),
    )
    assert fmt.tags == (f"<{tag}>", f"</{tag}>")
    assert fmt.controls == rf.ReasoningControls(f"<{tag}> ", f"<{tag}>  </{tag}>\n", f" </{tag}>\n")


def test_confirmed_unstructured_template_is_not_unknown():
    history = "ASSISTANT " + rf.PROBE_CONTENT
    fmt = rf.format_from_probes(on="ASSISTANT ", off="ASSISTANT ", history=history, empty_history=history)
    assert fmt.status == "none"
    assert rf.prepare_prompt("PREFIX", fmt, reasoning=True) == rf.PreparedPrompt("PREFIX", "content")


@pytest.mark.parametrize(
    "failure",
    [
        "missing",
        "duplicate",
        "unpaired",
        "unsupported",
        "different_empty",
        "toggle_only",
    ],
)
def test_ambiguous_probes_cannot_authorize_generation(failure):
    renders = _renders("qwen")
    if failure == "missing":
        renders["history"] = ""
    elif failure == "duplicate":
        renders["history"] += rf.PROBE_REASONING
    elif failure == "unpaired":
        renders["history"] = renders["history"].replace("</think>", "</reasoning>")
        renders["empty_history"] = renders["history"].replace(rf.PROBE_REASONING, "")
    elif failure == "unsupported":
        renders["history"] = "analysis:" + rf.PROBE_REASONING + " final:" + rf.PROBE_CONTENT
        renders["empty_history"] = renders["history"].replace(rf.PROBE_REASONING, "")
    elif failure == "different_empty":
        renders["empty_history"] += "extra bytes"
    else:
        renders["history"] = renders["empty_history"] = rf.PROBE_CONTENT
    fmt = rf.format_from_probes(**renders)
    assert fmt.status == "unknown"
    with pytest.raises(rf.ReasoningFormatError, match="Cannot determine"):
        rf.prepare_prompt("PROMPT", fmt, reasoning=True)


def _client(monkeypatch, current):
    client = LLMClient("http://test/v1", completion_mode="text", retry=RetryPolicy(count=0))
    calls = {"props": 0, "renders": 0, "completion": 0}

    async def props(root):
        calls["props"] += 1
        return (FIXTURES / f"{current[0]}.jinja").read_text()

    async def render(root, messages, chat_template_kwargs=None):
        calls["renders"] += 1
        if messages[-1]["role"] == "assistant":
            name = "history" if messages[-1].get("reasoning_content") else "empty_history"
        else:
            name = "on" if chat_template_kwargs["enable_thinking"] else "off"
        return _renders(current[0])[name]

    async def completion(url, body):
        calls["completion"] += 1
        calls["body"] = body
        yield {"content": "Blue", "stop": True}

    monkeypatch.setattr(client, "_fetch_chat_template", props)
    monkeypatch.setattr(client, "_apply_template", render)
    monkeypatch.setattr(client, "_stream_completion", completion)
    return client, calls


async def test_profile_reused_but_model_swaps_reprobed(monkeypatch):
    current = ["qwen"]
    client, calls = _client(monkeypatch, current)
    first = await client._reasoning_format("http://test")
    assert await client._reasoning_format("http://test") is first
    assert calls["renders"] == 4
    assert calls["props"] == 3  # before/after discovery, then a cache validation
    current[0] = "muse"
    assert (await client._reasoning_format("http://test")).channel
    assert calls["renders"] == 8


async def test_model_change_during_discovery_is_not_cached(monkeypatch):
    current = ["qwen"]
    client, _ = _client(monkeypatch, current)
    real = client._apply_template

    async def swap(*args, **kwargs):
        rendered = await real(*args, **kwargs)
        current[0] = "muse"
        return rendered

    monkeypatch.setattr(client, "_apply_template", swap)
    with pytest.raises(rf.ReasoningFormatError):
        await client._reasoning_format("http://test")
    assert client._reasoning_profile is None


async def test_metadata_outage_never_uses_cached_profile_or_emits_raw_content(
    monkeypatch,
):
    client, calls = _client(monkeypatch, ["qwen"])
    await client._reasoning_format("http://test")

    async def unavailable(root):
        raise httpx.ConnectError("metadata unavailable")

    monkeypatch.setattr(client, "_fetch_chat_template", unavailable)
    with pytest.raises(httpx.ConnectError):
        _ = [event async for event in client.complete(messages=[{"role": "user", "content": "Hi"}], model="m")]
    assert calls["completion"] == 0


@pytest.mark.parametrize(
    "body",
    [
        "null",
        "[]",
        "{}",
        '{"chat_template":null}',
        '{"chat_template":42}',
        '{"chat_template":""}',
        "bad json",
    ],
)
async def test_invalid_props_is_an_error_not_a_nonreasoning_model(monkeypatch, body):
    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    client = LLMClient("http://test/v1", completion_mode="text")
    with pytest.raises(rf.ReasoningFormatError, match="metadata"):
        await client._fetch_chat_template("http://test")


@pytest.mark.parametrize("name", ["qwen", "gemma", "muse"])
async def test_captured_render_through_constrained_transport(monkeypatch, name):
    client, calls = _client(monkeypatch, [name])
    events = [
        event
        async for event in client.complete(
            messages=[{"role": "user", "content": "Hi"}],
            model="m",
            grammar='root ::= "Blue"',
            **reasoning_cfg(True),
        )
    ]
    assert events[-1]["message"]["content"] == "Blue"
    assert "reasoning_content" not in events[-1]["message"]
    fmt = rf.format_from_probes(**_renders(name))
    assert rf.prompt_state(calls["body"]["prompt"], fmt) == "content"
