"""Unit tests for forced_tool_call: tools assembly, kv recording,
pass_id reasoning gating, and graceful degradation on every failure
path."""

from __future__ import annotations

from typing import Any, AsyncIterator

from backend.secondary_workflows._forced_call import forced_tool_call
from backend.tool_defs import STANDALONE_TOOLS, TOOLS


_TOOL_NAME = "editor_rewrite"
_SETTINGS = {"model_name": "test-model"}


class _RecordingTracker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list, list | None, str]] = []

    def record(self, label: str, messages: list, tools: list | None, model: str = "") -> None:
        self.calls.append((label, messages, tools, model))

    def record_usage(self, label: str, usage: dict | None) -> None:
        pass


class _FakeClient:
    """Drives `client.complete` with a programmable event stream."""

    def __init__(self, events: list[dict], raise_on_stream: Exception | None = None) -> None:
        self._events = events
        self._raise = raise_on_stream
        self.complete_kwargs: dict[str, Any] | None = None

    async def complete(self, **kwargs) -> AsyncIterator[dict]:
        self.complete_kwargs = kwargs
        if self._raise is not None:
            raise self._raise
        for ev in self._events:
            yield ev


def _done_event_with_tool_call(name: str, args: dict) -> dict:
    return {
        "type": "done",
        "message": {
            "tool_calls": [
                {"function": {"name": name, "arguments": args}},
            ]
        },
    }


async def _collect(gen: AsyncIterator[dict]) -> list[dict]:
    return [item async for item in gen]


class TestKVTracker:
    async def test_kv_tracker_none_does_not_record(self):
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "x"})])
        out = await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                kv_tracker=None,
            )
        )
        assert out == [{"type": "result", "args": {"rewritten_text": "x"}}]

    async def test_kv_tracker_records_with_pass_id_label(self):
        tracker = _RecordingTracker()
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "x"})])
        await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                pass_id="wf:p1",
                kv_tracker=tracker,
            )
        )
        assert len(tracker.calls) == 1
        label, _, tools, model = tracker.calls[0]
        assert label == "wf:p1"
        assert model == "test-model"
        assert len(tools) == 1
        assert tools[0]["function"]["name"] == _TOOL_NAME

    async def test_kv_tracker_default_label_when_no_pass_id(self):
        tracker = _RecordingTracker()
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
        await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                kv_tracker=tracker,
            )
        )
        assert tracker.calls[0][0] == f"forced:{_TOOL_NAME}"


class TestReasoningForwarding:
    async def test_pass_id_set_forwards_reasoning_deltas(self):
        client = _FakeClient(
            [
                {"type": "reasoning", "delta": "thinking..."},
                {"type": "reasoning", "delta": " more"},
                _done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "x"}),
            ]
        )
        out = await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                pass_id="wf:p1",
            )
        )
        assert out[:2] == [
            {"event": "reasoning", "data": {"pass": "wf:p1", "delta": "thinking..."}},
            {"event": "reasoning", "data": {"pass": "wf:p1", "delta": " more"}},
        ]
        assert out[-1] == {"type": "result", "args": {"rewritten_text": "x"}}

    async def test_pass_id_none_suppresses_reasoning_deltas(self):
        client = _FakeClient(
            [
                {"type": "reasoning", "delta": "thinking..."},
                _done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "x"}),
            ]
        )
        out = await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                pass_id=None,
            )
        )
        assert out == [{"type": "result", "args": {"rewritten_text": "x"}}]


class TestToolsAssembly:
    async def test_enabled_tools_none_single_schema(self):
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
        await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                enabled_tools=None,
            )
        )
        tools = client.complete_kwargs["tools"]
        assert [t["function"]["name"] for t in tools] == [_TOOL_NAME]

    async def test_enabled_tools_dict_matches_enabled_schemas(self):
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
        await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                enabled_tools={
                    "editor_rewrite": True,
                    "editor_apply_patch": True,
                    "direct_scene": False,
                },
            )
        )
        names = [t["function"]["name"] for t in client.complete_kwargs["tools"]]
        # enabled_schemas walks TOOLS in registry insertion order; only the True entries survive.
        assert names == ["editor_apply_patch", "editor_rewrite"]

    async def test_standalone_forced_tool_appended_to_array(self):
        STANDALONE_TOOLS.add(_TOOL_NAME)
        try:
            client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
            await _collect(
                forced_tool_call(
                    client=client,
                    prefix=[],
                    tail_messages=[],
                    tool_name=_TOOL_NAME,
                    settings=_SETTINGS,
                    enabled_tools={"editor_apply_patch": True},
                )
            )
            names = [t["function"]["name"] for t in client.complete_kwargs["tools"]]
            assert _TOOL_NAME in names
            assert "editor_apply_patch" in names
        finally:
            STANDALONE_TOOLS.discard(_TOOL_NAME)

    async def test_force_tool_missing_from_enabled_dict_appended(self):
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
        await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                enabled_tools={"editor_apply_patch": True, "editor_rewrite": False},
            )
        )
        names = [t["function"]["name"] for t in client.complete_kwargs["tools"]]
        assert _TOOL_NAME in names

    async def test_wrapped_prefix_unwrapped_to_plain_dicts(self):
        """A workflow that passes ``pre_ctx.prefix`` (tuple of
        MappingProxyType) must end up with plain dicts in the messages
        list -- json.dumps fails on MappingProxyType, so this is the only
        way prefix bytes match what the pipeline serializes."""
        import json
        from backend.secondary_workflows.contracts import _readonly

        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
        wrapped_prefix = _readonly([{"role": "system", "content": "x"}])
        wrapped_tail = _readonly([{"role": "user", "content": "y"}])
        tracker = _RecordingTracker()
        await _collect(
            forced_tool_call(
                client=client,
                prefix=wrapped_prefix,
                tail_messages=wrapped_tail,
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
                kv_tracker=tracker,
            )
        )
        messages = client.complete_kwargs["messages"]
        # Every entry must be a plain dict so httpx + json.dumps succeed.
        for m in messages:
            assert type(m) is dict
            json.dumps(m)  # raises if any wrapper leaked through
        assert messages == [
            {"role": "system", "content": "x"},
            {"role": "user", "content": "y"},
        ]
        # KV tracker also receives plain dicts.
        recorded_messages = tracker.calls[0][1]
        for m in recorded_messages:
            assert type(m) is dict

    async def test_messages_concatenate_prefix_and_tail(self):
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
        prefix = ({"role": "system", "content": "s"},)
        tail = ({"role": "user", "content": "u"},)
        await _collect(
            forced_tool_call(
                client=client,
                prefix=prefix,
                tail_messages=tail,
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
            )
        )
        assert client.complete_kwargs["messages"] == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]

    async def test_tool_choice_forwarded(self):
        client = _FakeClient([_done_event_with_tool_call(_TOOL_NAME, {})])
        await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
            )
        )
        assert client.complete_kwargs["tool_choice"] == TOOLS[_TOOL_NAME]["choice"]


class TestGracefulDegradation:
    async def test_tool_call_missing_yields_empty_args(self):
        client = _FakeClient([{"type": "done", "message": {"content": "no calls"}}])
        out = await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
            )
        )
        assert out == [{"type": "result", "args": {}}]

    async def test_wrong_tool_name_in_response_falls_back_to_empty(self):
        client = _FakeClient(
            [
                _done_event_with_tool_call("not_the_one", {"x": 1}),
            ]
        )
        out = await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
            )
        )
        assert out == [{"type": "result", "args": {}}]

    async def test_client_complete_raises_yields_empty_args(self):
        client = _FakeClient([], raise_on_stream=RuntimeError("network broke"))
        out = await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
            )
        )
        assert out == [{"type": "result", "args": {}}]

    async def test_parse_failure_yields_empty_args(self, monkeypatch):
        def _raises(_msg):
            raise ValueError("corrupt")

        monkeypatch.setattr("backend.secondary_workflows._forced_call.parse_tool_calls", _raises)
        client = _FakeClient([{"type": "done", "message": {"tool_calls": []}}])
        out = await _collect(
            forced_tool_call(
                client=client,
                prefix=[],
                tail_messages=[],
                tool_name=_TOOL_NAME,
                settings=_SETTINGS,
            )
        )
        assert out == [{"type": "result", "args": {}}]
