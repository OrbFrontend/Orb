"""Unit tests for forced_tool_call: tools assembly, kv recording,
pass_id reasoning gating, and graceful degradation on every failure
path."""

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from backend.inference import endpoint_profiles as ep
from backend.prompting.tool_catalog import TOOLS, register_tool, require_tool
from backend.workflows._forced_call import forced_tool_call
from backend.workflows.contracts import readonly_view

_TOOL_NAME = "editor_rewrite"
_SETTINGS = {"model_name": "test-model"}
_RESULT_X = [{"type": "result", "args": {"rewritten_text": "x"}}]
_EMPTY = [{"type": "result", "args": {}}]


class _RecordingTracker:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list, list | None, str]] = []
        self.lanes: list[tuple[str, str, str]] = []

    def record(
        self, label: str, messages: list, tools: list | None, model: str = "", endpoint: str = "", shape: str = ""
    ) -> None:
        self.calls.append((label, messages, tools, model))
        self.lanes.append((endpoint, model, shape))

    def record_usage(self, label: str, usage: dict | None) -> None:
        pass


class _ReplayClient:
    """Serves one programmed event list per ``complete`` call, recording each (or raising *error*)."""

    def __init__(self, *streams: list[dict], base_url: str | None = None, error: Exception | None = None) -> None:
        self._streams = streams
        self._error = error
        self.seen: list[dict[str, Any]] = []
        if base_url is not None:
            self.base_url = base_url

    @property
    def complete_kwargs(self) -> dict[str, Any]:
        return self.seen[-1]

    @property
    def tool_names(self) -> list[str]:
        return [t["function"]["name"] for t in self.complete_kwargs["tools"]]

    async def complete(self, **kwargs) -> AsyncIterator[dict]:
        self.seen.append(kwargs)
        if self._error is not None:
            raise self._error
        for ev in self._streams[len(self.seen) - 1]:
            yield ev


def _done_event_with_tool_call(name: str, args: dict) -> dict:
    return {"type": "done", "message": {"tool_calls": [{"function": {"name": name, "arguments": args}}]}}


def _client(args: dict | None = None, name: str = _TOOL_NAME, **kwargs) -> _ReplayClient:
    return _ReplayClient([_done_event_with_tool_call(name, {} if args is None else args)], **kwargs)


async def _call(client, *, prefix=(), tail_messages=(), settings=_SETTINGS, **kwargs) -> list[dict]:
    gen = forced_tool_call(
        client=client, prefix=prefix, tail_messages=tail_messages, tool_name=_TOOL_NAME, settings=settings, **kwargs
    )
    return [item async for item in gen]


class TestKVTracker:
    async def test_kv_tracker_none_does_not_record(self):
        assert await _call(_client({"rewritten_text": "x"}), kv_tracker=None) == _RESULT_X

    async def test_kv_tracker_records_with_pass_id_label(self):
        tracker = _RecordingTracker()
        await _call(_client({"rewritten_text": "x"}), pass_id="wf:p1", kv_tracker=tracker)
        [(label, _, tools, model)] = tracker.calls
        assert (label, model) == ("wf:p1", "test-model")
        assert [tool["function"]["name"] for tool in tools] == [_TOOL_NAME]

    async def test_kv_tracker_default_label_when_no_pass_id(self):
        tracker = _RecordingTracker()
        await _call(_client(), kv_tracker=tracker)
        assert tracker.calls[0][0] == f"forced:{_TOOL_NAME}"

    @pytest.mark.parametrize(
        "base_url,shape,lane",
        [
            # In dual-model mode the agent and writer servers can share a model name; the endpoint keeps their lanes apart.
            ("https://api.example.com/v1", None, ("https://api.example.com/v1", "test-model", "")),
            (None, "format_consistency:voice_rewrite", ("", "test-model", "format_consistency:voice_rewrite")),
        ],
    )
    async def test_kv_tracker_records_the_lane(self, base_url, shape, lane):
        tracker = _RecordingTracker()
        await _call(_client({}, base_url=base_url), kv_tracker=tracker, **({"cache_shape": shape} if shape else {}))
        assert tracker.lanes == [lane]


class TestReasoningForwarding:
    async def test_pass_id_set_forwards_reasoning_deltas(self):
        client = _ReplayClient(
            [
                {"type": "reasoning", "delta": "thinking..."},
                {"type": "reasoning", "delta": " more"},
                _done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "x"}),
            ]
        )
        assert await _call(client, pass_id="wf:p1") == [
            {"event": "reasoning", "data": {"pass": "wf:p1", "delta": "thinking..."}},
            {"event": "reasoning", "data": {"pass": "wf:p1", "delta": " more"}},
            *_RESULT_X,
        ]

    async def test_pass_id_none_suppresses_reasoning_deltas(self):
        client = _ReplayClient(
            [{"type": "reasoning", "delta": "thinking..."}, _done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "x"})]
        )
        assert await _call(client, pass_id=None) == _RESULT_X


@pytest.mark.parametrize(
    "settings,budget",
    [
        ({**_SETTINGS, "max_tokens": 600}, 600),
        # Present only when a separate agent endpoint overlaid its model config; the forced call runs on that lane.
        ({**_SETTINGS, "max_tokens": 600, "agent_max_tokens": 32768}, 32768),
        (_SETTINGS, 4096),  # the column default
    ],
)
async def test_the_agent_lanes_configured_max_tokens_is_the_budget(settings, budget):
    client = _client({"rewritten_text": "x"})
    await _call(client, settings=settings)
    assert client.complete_kwargs["max_tokens"] == budget


class TestToolsAssembly:
    @pytest.mark.parametrize(
        "kwargs,base_url,names",
        [
            ({"enabled_tools": None}, None, [_TOOL_NAME]),
            # enabled_schemas walks TOOLS in registry insertion order; only the True entries survive.
            (
                {"enabled_tools": {"editor_rewrite": True, "editor_apply_patch": True, "direct_scene": False}},
                None,
                ["editor_apply_patch", "editor_rewrite"],
            ),
            # A forced tool missing from the enabled dict is appended.
            (
                {"enabled_tools": {"editor_apply_patch": True, "editor_rewrite": False}},
                None,
                ["editor_apply_patch", _TOOL_NAME],
            ),
            (
                {"offer_tools": ("editor_apply_patch", _TOOL_NAME)},
                "http://localhost:5000/v1",
                ["editor_apply_patch", _TOOL_NAME],
            ),
            # DeepSeek + thinking coerces the forced tool_choice to "auto"; a rival schema would then win, so only the forced
            # tool may ship.
            (
                {"offer_tools": ("editor_apply_patch", _TOOL_NAME), "model_name": "deepseek-v4-pro", "reasoning_on": True},
                "https://api.deepseek.com",
                [_TOOL_NAME],
            ),
        ],
    )
    async def test_the_tools_array(self, kwargs, base_url, names):
        client = _client(base_url=base_url)
        await _call(client, **kwargs)
        assert client.tool_names == names

    async def test_standalone_forced_tool_appended_to_array(self):
        tool = require_tool(_TOOL_NAME)
        register_tool(_TOOL_NAME, tool["schema"], tool["choice"], standalone=True)
        try:
            client = _client()
            await _call(client, enabled_tools={"editor_apply_patch": True})
            assert {_TOOL_NAME, "editor_apply_patch"} <= set(client.tool_names)
        finally:
            register_tool(_TOOL_NAME, tool["schema"], tool["choice"], standalone=False)

    async def test_offer_tools_retries_alone_when_forcing_is_ignored(self):
        """A provider that ignores tool_choice can only be caught by the reply: retry with the forced tool alone and remember
        the endpoint for the rest of the session."""
        client = _ReplayClient(
            [_done_event_with_tool_call("editor_apply_patch", {})],
            [_done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "ok"})],
            base_url="http://ignores-forcing.local",
        )
        try:
            out = await _call(client, offer_tools=("editor_apply_patch", _TOOL_NAME))
            assert out == [{"type": "result", "args": {"rewritten_text": "ok"}}]
            assert [[t["function"]["name"] for t in kw["tools"]] for kw in client.seen] == [
                ["editor_apply_patch", _TOOL_NAME],
                [_TOOL_NAME],
            ]
            # Learned: the next call skips the wasted first attempt.
            assert not ep.honors_forced_tool_choice("http://ignores-forcing.local", "test-model")
        finally:
            ep._FORCED_CHOICE_IGNORED.discard(("http://ignores-forcing.local", "test-model"))

    async def test_no_tool_call_at_all_does_not_brand_the_endpoint(self):
        """A reply with no tool call is not evidence that forcing was ignored.

        Truncation at max_tokens mid-reasoning, a content-only answer, or a provider-side finish_reason=error all land here;
        branding the endpoint on one of those would drop the shared two-tool blob for the rest of the session on a provider
        that does honor forcing. Degrade to empty args, no retry, nothing learned.
        """
        client = _ReplayClient(
            [{"type": "done", "message": {"content": "I'll think about it", "finish_reason": "length"}}],
            [_done_event_with_tool_call(_TOOL_NAME, {"rewritten_text": "unreachable"})],
            base_url="http://truncating.local",
        )
        assert await _call(client, offer_tools=("editor_apply_patch", _TOOL_NAME)) == _EMPTY
        assert len(client.seen) == 1
        assert ep.honors_forced_tool_choice("http://truncating.local", "test-model")

    async def test_enabled_tools_array_never_collapses(self):
        """The pipeline's blob is the shared KV prefix: a wrong tool in the reply degrades to empty args."""
        client = _client(name="editor_apply_patch", base_url="http://ignores-forcing.local")
        assert await _call(client, enabled_tools={"editor_apply_patch": True, "editor_rewrite": True}) == _EMPTY
        assert len(client.seen) == 1

    async def test_wrapped_prefix_unwrapped_to_plain_dicts(self):
        """``pre_ctx.prefix`` (MappingProxyType) must reach the client and tracker as plain dicts, or json.dumps fails."""
        client, tracker = _client(), _RecordingTracker()
        await _call(
            client,
            prefix=readonly_view([{"role": "system", "content": "x"}]),
            tail_messages=readonly_view([{"role": "user", "content": "y"}]),
            kv_tracker=tracker,
        )
        messages = client.complete_kwargs["messages"]
        assert messages == [{"role": "system", "content": "x"}, {"role": "user", "content": "y"}]
        for m in [*messages, *tracker.calls[0][1]]:
            assert type(m) is dict
            json.dumps(m)  # raises if any wrapper leaked through

    async def test_messages_tool_choice_and_tools_in_prompt_are_forwarded(self):
        for flag in (True, False):
            client = _client()
            await _call(
                client,
                prefix=({"role": "system", "content": "s"},),
                tail_messages=({"role": "user", "content": "u"},),
                tools_in_prompt=flag,
            )
            assert client.complete_kwargs["messages"] == [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
            assert client.complete_kwargs["tool_choice"] == TOOLS[_TOOL_NAME]["choice"]
            # False keeps the tool schema out of the server-rendered prompt (KV cache).
            assert client.complete_kwargs["tools_in_prompt"] is flag


class TestGracefulDegradation:
    @pytest.mark.parametrize(
        "client",
        [
            _ReplayClient([{"type": "done", "message": {"content": "no calls"}}]),
            _client({"x": 1}, name="not_the_one"),
            _ReplayClient(error=RuntimeError("network broke")),
        ],
        ids=["no-tool-call", "wrong-tool", "client-raises"],
    )
    async def test_a_failed_call_yields_empty_args(self, client):
        assert await _call(client) == _EMPTY

    async def test_raise_errors_lets_the_provider_error_through(self):
        with pytest.raises(RuntimeError, match="image input not supported"):
            await _call(_ReplayClient(error=RuntimeError("image input not supported")), raise_errors=True)

    async def test_parse_failure_yields_empty_args(self, monkeypatch):
        def _raises(_msg):
            raise ValueError("corrupt")

        monkeypatch.setattr("backend.workflows._forced_call.parse_tool_calls", _raises)
        assert await _call(_ReplayClient([{"type": "done", "message": {"tool_calls": []}}])) == _EMPTY
