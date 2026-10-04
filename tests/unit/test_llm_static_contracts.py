"""Guard the model-call contract through transports, caching, and consumers."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

_SOURCE = """\
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any, assert_type

from backend.core.llm_types import (
    CompletionDelta, CompletionEvent, CompletionMessage, ContentDelta,
    ParsedToolCall, ReasoningDelta, TokenProbability, TokenProbsEvent,
)
from backend.core.reasoning import mark_call_start, reasoning_delta_event
from backend.features.documents.continuation import DocumentContinuer, DocumentEvent
from backend.inference import CachedBase, LLMClient, forced_turn, parse_tool_calls
from backend.inference.anthropic import consume_stream
from backend.inference.chat_stream import ChatStream, consume_openai
from backend.inference.claude_code import ClaudeCodeClient
from backend.inference.cached_call import cached_complete
from backend.inference.text_completion import forced_tool_message, normalize_prob_records
from backend.pipeline.passes.writer import writer_pass
from backend.pipeline.state import TurnState

async def boundaries(
    client: LLMClient, cli: ClaudeCodeClient, base: CachedBase,
    payloads: AsyncIterable[str], document: DocumentContinuer,
    readonly: Mapping[str, Any], events: AsyncIterator[CompletionEvent],
) -> None:
    assert_type(client.complete([], "m"), AsyncIterator[CompletionEvent])
    assert_type(client.complete_raw("prompt", "m"), AsyncIterator[CompletionEvent])
    assert_type(cli.complete([], "m"), AsyncIterator[CompletionEvent])
    assert_type(cli.complete_raw("prompt", "m"), AsyncIterator[CompletionEvent])
    assert_type(cached_complete(client, label="test", messages=[], model="m"), AsyncIterator[CompletionEvent])
    assert_type(base.complete(client, label="test", trailing=[]), AsyncIterator[CompletionEvent])
    reply: CompletionMessage = {}
    assert_type(base.complete_into(client, reply, label="test", trailing=[]), AsyncIterator[ReasoningDelta])
    base.complete_into(client, reply, trailing=[])  # rejected
    base.complete_into(client, reply, label="test", trailing="wrong")  # rejected
    base.complete_into(client, {"content": 123}, label="test", trailing=[])  # rejected
    assert_type(mark_call_start(events), AsyncIterator[CompletionEvent])
    assert_type(reasoning_delta_event({"type": "reasoning", "delta": "x"}), ReasoningDelta)
    assert_type(parse_tool_calls(readonly), list[ParsedToolCall])
    assert_type(forced_tool_message("tool", "{}"), CompletionMessage)
    assert_type(normalize_prob_records([]), list[TokenProbability])
    assert_type(writer_pass(client, base, {}, "prompt"), AsyncIterator[ContentDelta | ReasoningDelta])
    assert_type(TurnState().calls, list[ParsedToolCall])
    assert_type(await forced_turn(client, "m", messages=[], tools=[], forced="tool", max_tokens=10, reasoning_on=False), CompletionMessage)
    async for event in document.stream("prompt", "m"):
        assert_type(event, DocumentEvent)
    kw = dict(forced=False, url="https://example.com", model="m", api_key="", is_aborted=lambda: False)
    assert_type(consume_openai(payloads, ChatStream(), forced=False, url="u", model="m", api_key="", is_aborted=lambda: False), AsyncIterator[CompletionDelta])
    assert_type(consume_stream(payloads, ChatStream(), forced=False, url="u", model="m", api_key="", is_aborted=lambda: False), AsyncIterator[CompletionDelta])
    async for event in events:
        if event["type"] == "done":
            assert_type(event["message"], CompletionMessage)
            event["delta"]  # rejected
        elif event["type"] == "token_probs":
            assert_type(event, TokenProbsEvent)
            assert_type(event["prob"], float)
            event["delta"]  # rejected
        else:
            assert_type(event["delta"], str)
            event["message"]  # rejected

async def declarations() -> AsyncIterator[CompletionEvent]:
    yield {"type": "content", "delta": "text"}
    yield {"type": "reasoning", "delta": "thought", "call_start": True}
    yield {"type": "token_probs", "token": "a", "prob": 0.5, "top": [{"t": "b", "p": 0.1}]}
    yield {"type": "done", "message": {}, "usage": None}
    yield {"type": "done", "message": {"tool_calls": [{"id": "t", "type": "function", "function": {"name": "custom", "arguments": "{}"}}]}, "usage": {"provider_extension": [1, 2]}}
    yield {"type": "contnet", "delta": "typo"}  # rejected
    yield {"type": "content", "delta": 123}  # rejected
    yield {"type": "reasoning", "delta": "x", "call_start": "yes"}  # rejected
    yield {"type": "token_probs", "token": "a", "prob": 0.5}  # rejected
    yield {"type": "done", "message": {"content": 123}, "usage": None}  # rejected
    yield {"type": "done", "message": {}, "usage": []}  # rejected
    yield {"type": "done", "message": {}}  # rejected
    yield {"type": "done", "message": {"finish_reasn": "stop"}, "usage": None}  # rejected
    yield {"type": "done", "message": {"tool_calls": [{"id": "t", "type": "function", "function": {"name": "custom", "arguments": {}}}]}, "usage": None}  # rejected

def parsed(call: ParsedToolCall) -> None:
    assert_type(call["name"], str)
    assert_type(call["arguments"], dict[str, Any])
    call["argumnts"]  # rejected
"""


def test_llm_contracts_survive_transport_cache_and_consumer_boundaries(tmp_path):
    source = tmp_path / "llm_contracts.py"
    source.write_text(_SOURCE, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pyright", "--project", str(ROOT / "pyrightconfig.json"), "--outputjson", str(source)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode in (0, 1), result.stdout + result.stderr
    errors = [d for d in json.loads(result.stdout)["generalDiagnostics"] if d["severity"] == "error"]
    expected = {i for i, line in enumerate(_SOURCE.splitlines()) if line.endswith("# rejected")}
    actual = {d["range"]["start"]["line"] for d in errors if Path(d["file"]) == source}
    assert actual == expected, json.dumps(errors, indent=2)
    assert all(Path(d["file"]) == source for d in errors), json.dumps(errors, indent=2)
