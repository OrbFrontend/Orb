"""
tests/unit/test_orchestrator.py

Unit tests for backend/orchestrator.py.

Test organisation
─────────────────
1.  Pure helpers          – _sub, build_prefix, build_style_injection,
                            build_tool_prompt, apply_tool_calls
2.  Async pass functions  – _agent_pass, _writer_pass, _refine_pass
                            (LLMClient mocked, no DB)
3.  _run_pipeline         – event ordering + KV-cache prefix invariant
                            (the two things that matter most)
4.  Public entry-points   – handle_turn, handle_regenerate
                            (LLMClient constructor + entire DB module mocked)

KV-cache invariant
──────────────────
The orchestrator builds `prefix` once per turn (system prompt + chat history)
and passes it to every LLM call unchanged.  Both the agent pass (complete())
and the writer pass (stream()) must receive `messages` whose leading slice is
identical to `prefix`.  That lets the inference server reuse cached KV entries
for all those tokens rather than recomputing them on every call.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from backend.orchestrator import (
    _sub,
    build_prefix,
    build_style_injection,
    build_tool_prompt,
    apply_tool_calls,
    _agent_pass,
    _writer_pass,
    _refine_pass,
    _run_pipeline,
    handle_turn,
    handle_regenerate,
)
from backend.llm_client import LLMClient


# ---------------------------------------------------------------------------
# Test utilities
# ---------------------------------------------------------------------------

async def collect(gen) -> list[dict]:
    """Drain an async generator into a list."""
    return [e async for e in gen]


def events_of(collected: list[dict], name: str) -> list[dict]:
    """Filter collected events by event name."""
    return [e for e in collected if e["event"] == name]


def event_names(collected: list[dict]) -> list[str]:
    return [e["event"] for e in collected]


def make_client(*, complete_return: dict | None = None, stream_tokens: tuple[str, ...] = ()) -> MagicMock:
    """
    Return a MagicMock shaped like LLMClient.

    • complete() – coroutine returning complete_return (default: empty dict → no tool calls)
    • stream()   – async generator yielding stream_tokens
    """
    client = MagicMock(spec=LLMClient)
    client.complete = AsyncMock(return_value=complete_return or {})
    tokens = list(stream_tokens)

    async def _stream(*args, **kwargs):
        for t in tokens:
            yield t

    client.stream = _stream
    return client


def capturing_stream(tokens: tuple[str, ...] = ()) -> tuple:
    """
    Return (stream_fn, calls) where *calls* accumulates one entry per invocation:
        {"messages": [...], "kwargs": {...}}
    Useful for asserting KV-cache prefix invariant.
    """
    calls: list[dict] = []
    token_list = list(tokens)

    async def _stream(*args, **kwargs):
        msgs = kwargs.get("messages") or (args[0] if args else [])
        calls.append({"messages": msgs, "kwargs": kwargs})
        for t in token_list:
            yield t

    return _stream, calls


def capturing_complete(return_value: dict | None = None) -> tuple:
    """
    Return (complete_fn, calls) where *calls* accumulates one entry per invocation:
        {"messages": [...], "kwargs": {...}}
    """
    calls: list[dict] = []
    rv = return_value or {}

    async def _complete(*args, **kwargs):
        msgs = kwargs.get("messages") or (args[0] if args else [])
        calls.append({"messages": msgs, "kwargs": kwargs})
        return rv

    return _complete, calls


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def settings(base_settings):
    return base_settings


@pytest.fixture
def director(base_director):
    return base_director


@pytest.fixture
def fragments(base_fragments):
    return base_fragments


@pytest.fixture
def prefix() -> list[dict]:
    """Representative prefix (system + two history turns)."""
    return [
        {"role": "system", "content": "You are Aria, a helpful assistant."},
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi there!"},
    ]


# ===========================================================================
# 1. Pure helper functions
# ===========================================================================

class TestSubstitution:
    def test_replaces_user_and_char(self):
        assert _sub("Hello {{user}}, I am {{char}}.", "Alice", "Aria") == "Hello Alice, I am Aria."

    def test_missing_user_name_falls_back(self):
        assert _sub("Hello {{user}}", "", "Aria") == "Hello User"

    def test_missing_char_name_falls_back(self):
        assert _sub("I am {{char}}", "Alice", "") == "I am Character"

    def test_none_input_returns_empty_string(self):
        assert _sub(None, "Alice", "Aria") == ""


class TestBuildPrefix:
    def test_returns_list_starting_with_system_message(self):
        result = build_prefix("System prompt", "Aria", "", "")
        assert result[0]["role"] == "system"
        assert "System prompt" in result[0]["content"]

    def test_substitutes_char_in_system_prompt(self):
        result = build_prefix("You are {{char}}.", "Aria", "", "")
        assert "You are Aria." in result[0]["content"]

    def test_appends_history_messages(self):
        history = [{"role": "user", "content": "Hey"}, {"role": "assistant", "content": "Hi"}]
        result = build_prefix("Sys", "Aria", "", "", messages=history)
        assert len(result) == 3  # system + 2 history
        assert result[1] == {"role": "user", "content": "Hey"}
        assert result[2] == {"role": "assistant", "content": "Hi"}

    def test_includes_char_persona_and_scenario(self):
        result = build_prefix("Sys", "Aria", "She is wise.", "A forest setting.", user_name="Bob")
        system_content = result[0]["content"]
        assert "She is wise." in system_content
        assert "A forest setting." in system_content

    def test_includes_example_dialogue_when_provided(self):
        result = build_prefix("Sys", "Aria", "", "", mes_example="Example dialogue here.")
        assert "Example dialogue here." in result[0]["content"]

    def test_includes_post_history_instructions(self):
        result = build_prefix("Sys", "Aria", "", "", post_history_instructions="Always speak formally.")
        assert "Always speak formally." in result[0]["content"]

    def test_empty_history_returns_only_system(self):
        result = build_prefix("Sys", "Aria", "", "")
        assert len(result) == 1
        assert result[0]["role"] == "system"


class TestBuildStyleInjection:
    def test_wraps_in_scene_direction_tag(self):
        result = build_style_injection([])
        assert result.startswith("<current_scene_direction>")
        assert result.endswith("</current_scene_direction>")

    def test_includes_active_style_names_and_prompts(self, fragments):
        active = [fragments[0]]  # "tense"
        result = build_style_injection(active)
        assert 'name="tense"' in result
        assert "Write with short, punchy sentences." in result

    def test_includes_deactivated_style_with_negative_prompt(self, fragments):
        deactivated = [fragments[0]]  # "tense" has a negative_prompt
        result = build_style_injection([], deactivated)
        assert 'deactivated="true"' in result
        assert "Avoid flowing, relaxed sentences." in result

    def test_omits_deactivated_style_without_negative_prompt(self, fragments):
        deactivated = [fragments[1]]  # "lyrical" has empty negative_prompt
        result = build_style_injection([], deactivated)
        assert "lyrical" not in result

    def test_multiple_active_styles_all_present(self, fragments):
        result = build_style_injection(fragments)
        assert 'name="tense"' in result
        assert 'name="lyrical"' in result


class TestBuildToolPrompt:
    def test_returns_empty_string_for_unknown_tool(self):
        assert build_tool_prompt("nonexistent_tool", "msg", [], []) == ""

    def test_set_writing_styles_includes_user_message(self, fragments):
        result = build_tool_prompt("set_writing_styles", "Let's fight!", ["tense"], fragments)
        assert "Let's fight!" in result

    def test_set_writing_styles_lists_available_fragments(self, fragments):
        result = build_tool_prompt("set_writing_styles", "msg", [], fragments)
        assert "tense" in result
        assert "lyrical" in result

    def test_rewrite_user_prompt_includes_user_message(self):
        result = build_tool_prompt("rewrite_user_prompt", "I nod.", [], [])
        assert "I nod." in result

    def test_contains_tool_name_in_instruction(self):
        result = build_tool_prompt("set_writing_styles", "msg", [], [])
        assert "set_writing_styles" in result


class TestApplyToolCalls:
    def test_set_writing_styles_replaces_active_list(self):
        calls = [{"name": "set_writing_styles", "arguments": {"style_ids": ["tense", "lyrical"]}}]
        styles, refined = apply_tool_calls(calls, [])
        assert styles == ["tense", "lyrical"]
        assert refined is None

    def test_rewrite_user_prompt_captures_refined_message(self):
        calls = [{"name": "rewrite_user_prompt", "arguments": {"refined_message": "I step forward boldly."}}]
        styles, refined = apply_tool_calls(calls, ["tense"])
        assert styles == ["tense"]  # styles unchanged
        assert refined == "I step forward boldly."

    def test_empty_refined_message_returns_none(self):
        calls = [{"name": "rewrite_user_prompt", "arguments": {"refined_message": ""}}]
        _, refined = apply_tool_calls(calls, [])
        assert refined is None

    def test_multiple_tool_calls_applied_in_order(self):
        calls = [
            {"name": "set_writing_styles", "arguments": {"style_ids": ["tense"]}},
            {"name": "rewrite_user_prompt", "arguments": {"refined_message": "Better message."}},
        ]
        styles, refined = apply_tool_calls(calls, [])
        assert styles == ["tense"]
        assert refined == "Better message."

    def test_empty_call_list_is_noop(self):
        styles, refined = apply_tool_calls([], ["tense"])
        assert styles == ["tense"]
        assert refined is None

    def test_set_writing_styles_clears_previous_styles(self):
        """Style list is replaced, not merged."""
        calls = [{"name": "set_writing_styles", "arguments": {"style_ids": ["lyrical"]}}]
        styles, _ = apply_tool_calls(calls, ["tense", "dramatic"])
        assert styles == ["lyrical"]


# ===========================================================================
# 2. Async pass functions
# ===========================================================================

class TestAgentPass:
    async def test_returns_updated_styles(self, settings, director, fragments, prefix):
        response = {
            "tool_calls": [{
                "function": {
                    "name": "set_writing_styles",
                    "arguments": '{"style_ids": ["tense"]}',
                }
            }]
        }
        client = make_client(complete_return=response)
        active_styles, _, calls, _, refined = await _agent_pass(
            client, prefix, "Hello", settings, director, fragments
        )
        assert "tense" in active_styles
        assert len(calls) == 1

    async def test_returns_refined_message_when_rewrite_called(self, settings, director, fragments, prefix):
        settings["enabled_tools"]["rewrite_user_prompt"] = True
        response = {
            "tool_calls": [{
                "function": {
                    "name": "rewrite_user_prompt",
                    "arguments": '{"refined_message": "I stride forward."}',
                }
            }]
        }
        client = make_client(complete_return=response)
        _, _, _, _, refined = await _agent_pass(
            client, prefix, "I go.", settings, director, fragments
        )
        assert refined == "I stride forward."

    async def test_empty_tool_names_skips_llm(self, settings, director, fragments, prefix):
        """When all tools are disabled, no LLM call is made."""
        settings["enabled_tools"] = {
            "set_writing_styles": False,
            "rewrite_user_prompt": False,
        }
        client = make_client()
        active_styles, raw, calls, _, _ = await _agent_pass(
            client, prefix, "Hello", settings, director, fragments,
            enabled_tools=settings["enabled_tools"],
        )
        client.complete.assert_not_called()
        assert calls == []

    async def test_llm_failure_is_logged_not_raised(self, settings, director, fragments, prefix):
        """An exception from complete() must not propagate; the pass returns gracefully."""
        client = MagicMock(spec=LLMClient)
        client.complete = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        # Should not raise
        active_styles, raw, calls, _, _ = await _agent_pass(
            client, prefix, "Hello", settings, director, fragments
        )
        assert "ERROR" in raw

    async def test_agent_messages_start_with_prefix(self, settings, director, fragments, prefix):
        """
        KV-cache: agent call messages must start with the shared prefix so the
        server can reuse cached key-value entries for those tokens.
        """
        complete_fn, calls = capturing_complete()
        client = MagicMock(spec=LLMClient)
        client.complete = complete_fn
        await _agent_pass(client, prefix, "Hello", settings, director, fragments)
        assert len(calls) >= 1
        for call in calls:
            assert call["messages"][: len(prefix)] == prefix, (
                "Agent pass must not prepend or alter messages before the prefix"
            )


class TestWriterPass:
    async def test_yields_tokens(self, settings, prefix):
        client = make_client(stream_tokens=("Hello", " world", "!"))
        tokens = []
        async for tok in _writer_pass(client, prefix, settings):
            tokens.append(tok)
        assert tokens == ["Hello", " world", "!"]

    async def test_yields_nothing_for_empty_stream(self, settings, prefix):
        client = make_client(stream_tokens=())
        tokens = [t async for t in _writer_pass(client, prefix, settings)]
        assert tokens == []

    async def test_passes_settings_params_to_stream(self, settings, prefix):
        settings["temperature"] = 0.9
        settings["max_tokens"] = 512
        called_kwargs: dict = {}

        async def _stream(*args, **kwargs):
            called_kwargs.update(kwargs)
            return
            yield  # make it an async generator

        client = MagicMock(spec=LLMClient)
        client.stream = _stream
        _ = [t async for t in _writer_pass(client, prefix, settings)]
        assert called_kwargs.get("temperature") == 0.9
        assert called_kwargs.get("max_tokens") == 512


class TestRefinePass:
    async def test_returns_refined_text_when_tool_called(self, settings, prefix):
        response = {
            "tool_calls": [{
                "function": {
                    "name": "refine_assistant_output",
                    "arguments": '{"refined_output": "Polished text."}',
                }
            }]
        }
        client = make_client(complete_return=response)
        refined, raw, ms = await _refine_pass(client, prefix, "user msg", "draft text", settings)
        assert refined == "Polished text."

    async def test_returns_none_when_tool_not_called(self, settings, prefix):
        client = make_client(complete_return={})
        refined, _, _ = await _refine_pass(client, prefix, "user msg", "draft text", settings)
        assert refined is None

    async def test_returns_none_when_refined_output_empty(self, settings, prefix):
        response = {
            "tool_calls": [{
                "function": {
                    "name": "refine_assistant_output",
                    "arguments": '{"refined_output": ""}',
                }
            }]
        }
        client = make_client(complete_return=response)
        refined, _, _ = await _refine_pass(client, prefix, "user msg", "draft text", settings)
        assert refined is None

    async def test_llm_failure_returns_none_not_raises(self, settings, prefix):
        client = MagicMock(spec=LLMClient)
        client.complete = AsyncMock(side_effect=RuntimeError("LLM error"))
        refined, raw, _ = await _refine_pass(client, prefix, "user msg", "draft text", settings)
        assert refined is None
        assert "ERROR" in raw

    async def test_refine_messages_start_with_prefix(self, settings, prefix):
        """KV-cache: refine call messages must start with the shared prefix."""
        complete_fn, calls = capturing_complete()
        client = MagicMock(spec=LLMClient)
        client.complete = complete_fn
        await _refine_pass(client, prefix, "user msg", "draft text", settings)
        assert len(calls) == 1
        assert calls[0]["messages"][: len(prefix)] == prefix


# ===========================================================================
# 3. _run_pipeline: event ordering + KV-cache prefix invariant
# ===========================================================================

class TestRunPipelineEventOrdering:
    """
    _run_pipeline is an async generator that emits a strict sequence of events.
    These tests assert that sequence under various configurations.

    Full sequence (agent ON, rewrite ON, refine ON):
        director_start → prompt_rewritten → director_done → token… → writer_rewrite → _result

    Simplified (agent ON, no rewrite, no refine):
        director_start → director_done → token… → _result

    Agent OFF:
        director_done → token… → _result
    """

    async def test_basic_order_agent_on_no_rewrite_no_refine(self, settings, director, fragments, prefix):
        """director_start → director_done → token(s) → _result."""
        client = make_client(stream_tokens=("Hello", " world"))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        names = event_names(events)
        assert names[0] == "director_start"
        assert "director_done" in names
        assert names.index("director_done") < names.index("_result")
        # All tokens come after director_done and before _result
        done_idx = names.index("director_done")
        result_idx = names.index("_result")
        token_indices = [i for i, n in enumerate(names) if n == "token"]
        assert all(done_idx < i < result_idx for i in token_indices)

    async def test_prompt_rewritten_emitted_before_director_done(self, settings, director, fragments, prefix):
        """When the agent rewrites the prompt, prompt_rewritten comes before director_done."""
        settings["enabled_tools"]["rewrite_user_prompt"] = True
        response = {
            "tool_calls": [{
                "function": {
                    "name": "rewrite_user_prompt",
                    "arguments": '{"refined_message": "I stride forward boldly."}',
                }
            }]
        }
        client = make_client(complete_return=response, stream_tokens=("Story.",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "I go."))
        names = event_names(events)
        assert "prompt_rewritten" in names
        assert names.index("prompt_rewritten") < names.index("director_done")

    async def test_director_start_always_before_director_done(self, settings, director, fragments, prefix):
        client = make_client(stream_tokens=("x",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        names = event_names(events)
        assert names.index("director_start") < names.index("director_done")

    async def test_no_director_start_when_agent_disabled(self, settings, director, fragments, prefix):
        settings["enable_agent"] = 0
        client = make_client(stream_tokens=("x",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        names = event_names(events)
        assert "director_start" not in names
        assert "director_done" in names

    async def test_writer_rewrite_emitted_after_last_token(self, settings, director, fragments, prefix):
        """writer_rewrite must come after all token events."""
        settings["enabled_tools"]["refine_assistant_output"] = True
        settings["enable_agent"] = 1
        refine_response = {
            "tool_calls": [{
                "function": {
                    "name": "refine_assistant_output",
                    "arguments": '{"refined_output": "Polished."}',
                }
            }]
        }
        client = MagicMock(spec=LLMClient)
        # complete() used for both agent and refine passes
        client.complete = AsyncMock(return_value=refine_response)

        async def _stream(*args, **kwargs):
            for t in ("Token1", "Token2"):
                yield t

        client.stream = _stream
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        names = event_names(events)
        assert "writer_rewrite" in names
        last_token_idx = max((i for i, n in enumerate(names) if n == "token"), default=-1)
        rewrite_idx = names.index("writer_rewrite")
        assert rewrite_idx > last_token_idx

    async def test_no_writer_rewrite_when_refine_returns_nothing(self, settings, director, fragments, prefix):
        settings["enabled_tools"]["refine_assistant_output"] = True
        client = make_client(complete_return={}, stream_tokens=("text",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        names = event_names(events)
        assert "writer_rewrite" not in names

    async def test_no_prompt_rewritten_when_not_rewritten(self, settings, director, fragments, prefix):
        client = make_client(complete_return={}, stream_tokens=("text",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        names = event_names(events)
        assert "prompt_rewritten" not in names

    async def test_result_event_is_last(self, settings, director, fragments, prefix):
        client = make_client(stream_tokens=("x",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        assert events[-1]["event"] == "_result"

    async def test_director_done_contains_active_styles(self, settings, director, fragments, prefix):
        response = {
            "tool_calls": [{
                "function": {
                    "name": "set_writing_styles",
                    "arguments": '{"style_ids": ["tense"]}',
                }
            }]
        }
        client = make_client(complete_return=response, stream_tokens=("x",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        done_evt = events_of(events, "director_done")[0]
        assert "tense" in done_evt["data"]["active_styles"]

    async def test_token_events_accumulate_full_response(self, settings, director, fragments, prefix):
        client = make_client(stream_tokens=("Hello", " world", "!"))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        tokens = [e["data"] for e in events if e["event"] == "token"]
        assert "".join(tokens) == "Hello world!"

    async def test_result_contains_resp_text(self, settings, director, fragments, prefix):
        client = make_client(stream_tokens=("Hello", " world"))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        result = events[-1]["data"]
        assert result["resp_text"] == "Hello world"

    async def test_result_effective_msg_updated_when_rewritten(self, settings, director, fragments, prefix):
        """When the agent rewrites the prompt, the result should reflect the new message."""
        settings["enabled_tools"]["rewrite_user_prompt"] = True
        response = {
            "tool_calls": [{
                "function": {
                    "name": "rewrite_user_prompt",
                    "arguments": '{"refined_message": "I stride forward boldly."}',
                }
            }]
        }
        client = make_client(complete_return=response, stream_tokens=("x",))
        events = await collect(_run_pipeline(client, settings, director, fragments, prefix, "I go."))
        result = events[-1]["data"]
        assert result["effective_msg"] == "I stride forward boldly."
        assert result["refined_msg"] == "I stride forward boldly."


class TestRunPipelineKVCacheInvariant:
    """
    The shared prefix must appear unchanged at the start of every LLM call
    (both agent/complete and writer/stream) so the inference server can reuse
    KV-cache entries across the two passes.
    """

    async def test_agent_complete_messages_start_with_prefix(self, settings, director, fragments, prefix):
        complete_fn, complete_calls = capturing_complete()
        stream_fn, _ = capturing_stream(("x",))
        client = MagicMock(spec=LLMClient)
        client.complete = complete_fn
        client.stream = stream_fn

        await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))

        assert complete_calls, "complete() should have been called at least once"
        for call in complete_calls:
            actual_prefix = call["messages"][: len(prefix)]
            assert actual_prefix == prefix, (
                f"Agent call messages did not start with the shared prefix.\n"
                f"Expected: {prefix}\nGot prefix slice: {actual_prefix}"
            )

    async def test_writer_stream_messages_start_with_prefix(self, settings, director, fragments, prefix):
        stream_fn, stream_calls = capturing_stream(("Hello",))
        client = MagicMock(spec=LLMClient)
        client.complete = AsyncMock(return_value={})
        client.stream = stream_fn

        await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))

        assert stream_calls, "stream() should have been called exactly once"
        actual_prefix = stream_calls[0]["messages"][: len(prefix)]
        assert actual_prefix == prefix, (
            f"Writer call messages did not start with the shared prefix.\n"
            f"Expected: {prefix}\nGot prefix slice: {actual_prefix}"
        )

    async def test_agent_and_writer_share_identical_prefix(self, settings, director, fragments, prefix):
        """
        The prefix slice seen by the agent and the writer must be the same object
        contents.  This is the core invariant for KV-cache reuse: both passes
        start with identical tokens so the server only needs to compute them once.
        """
        complete_fn, complete_calls = capturing_complete()
        stream_fn, stream_calls = capturing_stream(("x",))
        client = MagicMock(spec=LLMClient)
        client.complete = complete_fn
        client.stream = stream_fn

        await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))

        assert complete_calls and stream_calls
        agent_prefix_slice = complete_calls[0]["messages"][: len(prefix)]
        writer_prefix_slice = stream_calls[0]["messages"][: len(prefix)]
        assert agent_prefix_slice == writer_prefix_slice, (
            "Agent and writer received different prefix slices — KV-cache cannot be shared"
        )

    async def test_prefix_not_mutated_between_passes(self, settings, director, fragments, prefix):
        """
        Passing prefix + [...] must not mutate the original prefix list.
        If it did, subsequent passes would see a polluted prefix.
        """
        original_prefix = [dict(m) for m in prefix]  # deep copy to compare
        client = make_client(stream_tokens=("x",))
        await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))
        assert prefix == original_prefix, "prefix list was mutated during pipeline execution"

    async def test_prefix_shared_across_multiple_agent_tool_calls(self, settings, director, fragments, prefix):
        """
        When multiple agent tools are enabled, every complete() call must still
        begin with the same prefix.
        """
        settings["enabled_tools"]["rewrite_user_prompt"] = True
        complete_fn, complete_calls = capturing_complete()
        stream_fn, _ = capturing_stream(("x",))
        client = MagicMock(spec=LLMClient)
        client.complete = complete_fn
        client.stream = stream_fn

        await collect(_run_pipeline(client, settings, director, fragments, prefix, "I go."))

        assert len(complete_calls) >= 2, "Expected at least two agent tool calls"
        for i, call in enumerate(complete_calls):
            actual_prefix = call["messages"][: len(prefix)]
            assert actual_prefix == prefix, (
                f"Agent call #{i} messages did not start with the shared prefix"
            )

    async def test_writer_receives_only_enabled_schemas(self, settings, director, fragments, prefix):
        """
        Writer stream() must receive exactly the enabled subset of tool schemas —
        not ALL_SCHEMAS, not an empty list.  Tool schemas are serialised into the
        prompt; sending a different set than the agent invalidates the KV cache.
        """
        settings["enabled_tools"] = {
            "set_writing_styles": True,
            "rewrite_user_prompt": False,
            "refine_assistant_output": False,
        }
        stream_fn, stream_calls = capturing_stream(("x",))
        client = MagicMock(spec=LLMClient)
        client.complete = AsyncMock(return_value={})
        client.stream = stream_fn

        await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))

        assert stream_calls
        writer_tools = stream_calls[0]["kwargs"].get("tools")
        assert writer_tools is not None, "Writer must receive a tools list when agent is enabled"
        tool_names_sent = [t["function"]["name"] for t in writer_tools]
        assert tool_names_sent == ["set_writing_styles"], (
            f"Writer must receive only the enabled schemas. Got: {tool_names_sent}"
        )

    async def test_agent_and_writer_receive_same_tools_list(self, settings, director, fragments, prefix):
        """
        Tools are serialised into the prompt alongside messages.  For the KV cache
        to be reusable, agent complete() and writer stream() must see an identical
        tools list — any difference invalidates the cached prefix.
        """
        settings["enabled_tools"] = {
            "set_writing_styles": True,
            "rewrite_user_prompt": False,
            "refine_assistant_output": False,
        }
        complete_fn, complete_calls = capturing_complete()
        stream_fn, stream_calls = capturing_stream(("x",))
        client = MagicMock(spec=LLMClient)
        client.complete = complete_fn
        client.stream = stream_fn

        await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))

        assert complete_calls and stream_calls
        agent_tools = complete_calls[0]["kwargs"].get("tools")
        writer_tools = stream_calls[0]["kwargs"].get("tools")
        assert agent_tools == writer_tools, (
            "Agent and writer received different tools lists — KV-cache cannot be shared.\n"
            f"Agent tools:  {[t['function']['name'] for t in (agent_tools or [])]}\n"
            f"Writer tools: {[t['function']['name'] for t in (writer_tools or [])]}"
        )

    async def test_no_tools_field_when_agent_disabled(self, settings, director, fragments, prefix):
        """
        When the agent is disabled, the writer must not receive a tools field at all.
        Sending an empty list is not equivalent to omitting it — some servers treat
        them differently, and it still changes the serialised prompt token sequence.
        """
        settings["enable_agent"] = 0
        stream_fn, stream_calls = capturing_stream(("x",))
        client = MagicMock(spec=LLMClient)
        client.complete = AsyncMock(return_value={})
        client.stream = stream_fn

        await collect(_run_pipeline(client, settings, director, fragments, prefix, "Hi"))

        assert stream_calls
        assert "tools" not in stream_calls[0]["kwargs"], (
            "No tools field must be sent to the writer when the agent is disabled"
        )


# ===========================================================================
# 4. Public entry-points (DB + LLMClient mocked)
# ===========================================================================

# Minimal DB stubs used by handle_turn / handle_regenerate
_CONV = {
    "id": "conv-1",
    "character_name": "Aria",
    "character_scenario": "",
    "character_card_id": None,
    "active_leaf_id": None,
    "post_history_instructions": "",
}
_USER_MSG = {"id": 10, "conversation_id": "conv-1", "role": "user", "content": "Hello", "turn_index": 0, "parent_id": None}
_ASST_MSG = {"id": 11, "conversation_id": "conv-1", "role": "assistant", "content": "Hi!", "turn_index": 1, "parent_id": 10}


def make_db_mock(*, messages=None, conv=_CONV, director=None, fragments=None):
    """Return a MagicMock that looks like the database module."""
    m = MagicMock()
    m.get_settings = AsyncMock(return_value={
        "model_name": "test-model", "system_prompt": "Sys", "endpoint_url": "http://localhost",
        "api_key": "", "enable_agent": 1,
        "enabled_tools": {"set_writing_styles": True, "rewrite_user_prompt": False, "refine_assistant_output": False},
        "user_name": "Tester", "user_description": "",
    })
    m.get_conversation = AsyncMock(return_value=conv)
    m.get_messages = AsyncMock(return_value=messages or [])
    m.get_director_state = AsyncMock(return_value=director or {"active_styles": []})
    m.get_fragments = AsyncMock(return_value=fragments or [])
    m.get_character_card = AsyncMock(return_value=None)
    m.update_director_state = AsyncMock()
    m.add_message = AsyncMock(side_effect=[100, 101, 102, 103])  # unique IDs per call
    m.set_active_leaf = AsyncMock()
    m.update_message_content = AsyncMock()
    m.add_conversation_log = AsyncMock()
    m.get_message_by_id = AsyncMock(return_value=None)
    m._get_path_to_leaf = AsyncMock(return_value=[])
    m.get_styles_before_turn = AsyncMock(return_value=[])
    return m


class TestHandleTurn:
    async def test_yields_done_as_final_event(self):
        db_mock = make_db_mock()
        client = make_client(stream_tokens=("Hi",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            events = await collect(handle_turn("conv-1", "Hello"))
        assert events[-1]["event"] == "done"

    async def test_result_event_not_exposed_to_caller(self):
        db_mock = make_db_mock()
        client = make_client(stream_tokens=("Hi",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            events = await collect(handle_turn("conv-1", "Hello"))
        assert all(e["event"] != "_result" for e in events)

    async def test_yields_error_when_conversation_not_found(self):
        db_mock = make_db_mock()
        db_mock.get_conversation = AsyncMock(return_value=None)
        client = make_client()
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            events = await collect(handle_turn("missing", "Hello"))
        assert events[0]["event"] == "error"

    async def test_persists_user_and_assistant_messages(self):
        db_mock = make_db_mock()
        client = make_client(stream_tokens=("Response",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            await collect(handle_turn("conv-1", "Hello"))
        # add_message should be called twice: once for user, once for assistant
        assert db_mock.add_message.call_count == 2

    async def test_skip_user_persist_does_not_add_user_message(self):
        """
        skip_user_persist=True means the user message is already in the DB;
        we should not insert it again, only insert the assistant reply.
        """
        # Last message in history is the user message (already persisted)
        db_mock = make_db_mock(messages=[_USER_MSG])
        client = make_client(stream_tokens=("Response",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            await collect(handle_turn("conv-1", "Hello", skip_user_persist=True))
        # Only the assistant message should be added
        assert db_mock.add_message.call_count == 1
        role_arg = db_mock.add_message.call_args[0][1]
        assert role_arg == "assistant"

    async def test_updates_director_state_after_agent_pass(self):
        response = {
            "tool_calls": [{
                "function": {
                    "name": "set_writing_styles",
                    "arguments": '{"style_ids": ["tense"]}',
                }
            }]
        }
        db_mock = make_db_mock()
        client = make_client(complete_return=response, stream_tokens=("x",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            await collect(handle_turn("conv-1", "Hello"))
        db_mock.update_director_state.assert_called_once()
        _, saved_styles = db_mock.update_director_state.call_args[0]
        assert "tense" in saved_styles

    async def test_does_not_update_director_state_when_agent_disabled(self):
        db_mock = make_db_mock()
        db_mock.get_settings = AsyncMock(return_value={
            "model_name": "test-model", "system_prompt": "Sys",
            "endpoint_url": "http://localhost", "api_key": "",
            "enable_agent": 0, "enabled_tools": {},
            "user_name": "Tester", "user_description": "",
        })
        client = make_client(stream_tokens=("x",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            await collect(handle_turn("conv-1", "Hello"))
        db_mock.update_director_state.assert_not_called()


class TestHandleRegenerate:
    async def test_yields_done_as_final_event(self):
        db_mock = make_db_mock()
        db_mock.get_message_by_id = AsyncMock(side_effect=[_ASST_MSG, _USER_MSG])
        client = make_client(stream_tokens=("Regen",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            events = await collect(handle_regenerate("conv-1", 11))
        assert events[-1]["event"] == "done"

    async def test_yields_error_for_missing_conversation(self):
        db_mock = make_db_mock()
        db_mock.get_conversation = AsyncMock(return_value=None)
        client = make_client()
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            events = await collect(handle_regenerate("missing", 11))
        assert events[0]["event"] == "error"

    async def test_yields_error_for_wrong_role(self):
        """Target message must be an assistant message."""
        db_mock = make_db_mock()
        wrong_role_msg = {**_ASST_MSG, "role": "user"}
        db_mock.get_message_by_id = AsyncMock(return_value=wrong_role_msg)
        client = make_client()
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            events = await collect(handle_regenerate("conv-1", 10))
        assert events[0]["event"] == "error"

    async def test_restores_styles_from_before_original_turn(self):
        """
        Regeneration should use the styles that were active *before* the original
        turn, not the current director state — so the style context is identical
        to what the first generation used.
        """
        db_mock = make_db_mock()
        db_mock.get_message_by_id = AsyncMock(side_effect=[_ASST_MSG, _USER_MSG])
        db_mock.get_styles_before_turn = AsyncMock(return_value=["tense"])
        client = make_client(stream_tokens=("x",))

        captured_director: list[dict] = []

        original_run_pipeline = __import__(
            "backend.orchestrator", fromlist=["_run_pipeline"]
        )._run_pipeline

        async def patched_pipeline(client_, settings_, director_, *args, **kwargs):
            captured_director.append(director_)
            async for e in original_run_pipeline(client_, settings_, director_, *args, **kwargs):
                yield e

        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client), \
             patch("backend.orchestrator._run_pipeline", patched_pipeline):
            await collect(handle_regenerate("conv-1", 11))

        assert captured_director, "pipeline was not called"
        assert captured_director[0]["active_styles"] == ["tense"]

    async def test_new_assistant_message_added_as_sibling(self):
        """Regeneration creates a new assistant message (sibling branch), not in-place edit."""
        db_mock = make_db_mock()
        db_mock.get_message_by_id = AsyncMock(side_effect=[_ASST_MSG, _USER_MSG])
        client = make_client(stream_tokens=("New response",))
        with patch("backend.orchestrator.db", db_mock), \
             patch("backend.orchestrator.LLMClient", return_value=client):
            await collect(handle_regenerate("conv-1", 11))
        db_mock.add_message.assert_called_once()
        call_args = db_mock.add_message.call_args[0]
        assert call_args[1] == "assistant"
        assert call_args[2] == "New response"
