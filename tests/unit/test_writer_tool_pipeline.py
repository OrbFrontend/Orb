"""Per-lane schema assembly, the tail policy, and the Writer's downstream replay.

These are the pieces the KV-cache invariant rests on, so the assertions are
about *bytes and order* rather than about sets: "the writer and agent lanes
share a base" is a claim about object identity in single-model mode, and "a
selected schema perturbs the tail" is a claim about where in the tuple it lands.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from backend.core import WriterToolKey, WriterToolSpec
from backend.inference import CachedBase
from backend.pipeline.config import (
    _parameter_summary,
    _resolve_pipeline_config,
    _union_tool_schemas,
)
from backend.pipeline.passes.writer import (
    build_writer_content,
    standard_tool_calls,
    writer_pass,
    writer_tool_block,
)
from backend.pipeline.replay import WriterReplay
from backend.pipeline.state import TurnState, WriterToolPolicy
from backend.workflows.contracts import WriterToolBinding
from backend.workflows.registry import RegistrySnapshot

WIRE = "orb_writer_outcome-resolver--resolve_outcome"

SCHEMA = {
    "type": "function",
    "function": {
        "name": WIRE,
        "description": "Resolve an uncertain action.",
        "parameters": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "stakes": {"type": "string"}},
            "required": ["action"],
            "additionalProperties": False,
        },
    },
}


def _binding(name: str = WIRE, digest: str = "d1") -> WriterToolBinding:
    async def invoke(_request):  # pragma: no cover - never called in these tests
        raise AssertionError("not invoked")

    return WriterToolBinding(
        spec=WriterToolSpec(
            key=WriterToolKey("outcome-resolver", "resolve_outcome"),
            wire_name=name,
            label="Resolve outcome",
            schema={**SCHEMA, "function": {**SCHEMA["function"], "name": name}},
            content_digest=digest,
        ),
        invoke=invoke,
    )


def _policy(**kwargs) -> WriterToolPolicy:
    binding = _binding()
    return WriterToolPolicy(
        binding=binding,
        description=binding.spec.schema["function"]["description"],
        parameter_summary=_parameter_summary(binding.spec.schema["function"]["parameters"]),
        **kwargs,
    )


# ── the union ───────────────────────────────────────────────────────────────

AGENT = [
    {"type": "function", "function": {"name": "direct_scene", "parameters": {}}},
    {"type": "function", "function": {"name": "editor_apply_patch", "parameters": {}}},
]


def test_agent_order_is_preserved_and_the_writer_schema_is_appended():
    """A package with no Writer tool must produce byte-identical bytes to the
    ones Orb sent before this feature existed, and selecting one must perturb
    the tail rather than reshuffle the prefix."""
    assert _union_tool_schemas(AGENT, []) == tuple(AGENT)
    merged = _union_tool_schemas(AGENT, [SCHEMA])
    assert [t["function"]["name"] for t in merged] == ["direct_scene", "editor_apply_patch", WIRE]


def test_a_colliding_name_is_dropped_rather_than_shipped_twice():
    """Impossible by construction -- the derived namespace is prefixed -- but a
    provider silently binding one name to two schemas is not a failure worth
    discovering in production."""
    collide = {"type": "function", "function": {"name": "direct_scene", "parameters": {}}}
    merged = _union_tool_schemas(AGENT, [collide])
    assert [t["function"]["name"] for t in merged] == ["direct_scene", "editor_apply_patch"]


def test_the_union_is_deterministic_across_calls():
    assert _union_tool_schemas(AGENT, [SCHEMA]) == _union_tool_schemas(AGENT, [SCHEMA])


# ── the tail policy ─────────────────────────────────────────────────────────


def test_no_active_tool_renders_no_policy_block():
    assert writer_tool_block(WriterToolPolicy()) == ""


def test_the_policy_block_is_fixed_text_with_two_package_influenced_holes():
    block = writer_tool_block(_policy())
    assert f"call ONLY `{WIRE}`" in block
    assert "Call it at most once. Never call Director or Editor tools." in block
    assert "continue from that exact point without repeating prior prose" in block
    assert "What it does: Resolve an uncertain action." in block
    assert "It takes: action (required), stakes" in block


def test_the_no_tools_nudge_is_replaced_not_stacked():
    """Two instructions that disagree is worse than either alone."""
    with_tool = build_writer_content("", "", True, "hi", None, None, writer_tool_policy=_policy())
    assert "Do not use tool or function calls" not in with_tool
    without = build_writer_content("", "", True, "hi", None, None, writer_tool_policy=WriterToolPolicy())
    assert "Do not use tool or function calls" in without


def test_the_policy_is_the_final_text_after_the_user_message():
    content = build_writer_content("", "", True, "the user says hi", None, None, writer_tool_policy=_policy())
    assert content.index("the user says hi") < content.index("Writer tool policy")


def test_with_attachments_the_policy_is_a_final_text_part():
    parts = build_writer_content(
        "",
        "",
        True,
        "look",
        [{"mime_type": "image/png", "data_b64": "aGk="}],
        None,
        writer_tool_policy=_policy(),
    )
    assert isinstance(parts, list)
    assert [p["type"] for p in parts] == ["text", "image_url", "text"]
    assert "Writer tool policy" in parts[-1]["text"]
    assert "Writer tool policy" not in parts[0]["text"]


def test_without_a_policy_attachments_keep_the_historical_shape():
    parts = build_writer_content("", "", True, "look", [{"mime_type": "image/png", "data_b64": "aGk="}], None)
    assert [p["type"] for p in parts] == ["text", "image_url"]


# ── returned-call recognition ───────────────────────────────────────────────


def test_only_standard_structured_calls_are_recognised():
    assert standard_tool_calls({"tool_calls": [{"id": "1", "function": {"name": WIRE}}]})
    assert standard_tool_calls({"content": f'{{"name": "{WIRE}", "arguments": {{}}}}'}) == []
    assert standard_tool_calls({"content": f'<tool_call>{{"name": "{WIRE}"}}</tool_call>'}) == []
    assert standard_tool_calls({}) == []
    assert standard_tool_calls({"tool_calls": None}) == []


class _WriterLoopClient:
    completion_mode = "chat"

    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls = 0
        self.is_aborted = False

    def abort(self) -> None:
        self.is_aborted = True

    async def complete(self, **_kwargs):
        self.calls += 1
        response = self.responses.pop(0)
        content = response.get("content", "")
        if content:
            yield {"type": "content", "delta": content}
        yield {"type": "done", "message": {"role": "assistant", **response}}


def _loop_policy(invoke) -> WriterToolPolicy:
    base_binding = _binding()
    binding = WriterToolBinding(spec=base_binding.spec, invoke=invoke)
    return WriterToolPolicy(
        binding=binding,
        description="Resolve an uncertain action.",
        parameter_summary="action (required)",
    )


def _writer_tool_call() -> list[dict]:
    return [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": WIRE, "arguments": {"action": "try"}},
        }
    ]


async def test_cancellation_during_the_binding_does_not_start_a_continuation():
    client = _WriterLoopClient([{"content": "She tries. ", "tool_calls": _writer_tool_call()}])

    async def cancel(request):
        request.client.abort()
        raise RuntimeError("feature-local cancellation")

    events = [
        event
        async for event in writer_pass(
            client,
            CachedBase(prefix=(), tools=(SCHEMA,), model="m"),
            {"model_name": "m"},
            "request",
            policy=_loop_policy(cancel),
            turn_seed="attempt-1",
        )
    ]

    assert client.calls == 1
    assert [event for event in events if event["type"] == "tool_status"][-1]["running"] is False
    assert not any(event["type"] == "trace" for event in events)


async def test_a_failed_replayable_exchange_keeps_its_trace_for_downstream_calls():
    client = _WriterLoopClient(
        [
            {"content": "She tries. ", "tool_calls": _writer_tool_call()},
            {"content": "Nothing happens."},
        ]
    )

    async def fail(_request):
        raise RuntimeError("resolver failed")

    events = [
        event
        async for event in writer_pass(
            client,
            CachedBase(prefix=(), tools=(SCHEMA,), model="m"),
            {"model_name": "m"},
            "request",
            policy=_loop_policy(fail),
            turn_seed="attempt-1",
        )
    ]

    trace = next(event for event in events if event["type"] == "trace")
    assert trace["used_tool"] is True
    assert any(message.get("role") == "tool" for message in trace["messages"])


# ── downstream replay ───────────────────────────────────────────────────────


def test_a_turn_without_a_tool_call_replays_the_historical_pair():
    state = TurnState(writer_content="request", resp_text="draft")
    replay = state.writer_replay("draft", dual_model=False)
    assert replay.messages == (
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "draft"},
    )
    assert replay.canonical_draft_block == ""


def test_a_dual_model_lane_never_replays_the_tool_transcript():
    state = TurnState(writer_content="request", resp_text="a b")
    state.writer_tool_used = True
    state.writer_trace = [
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "a ", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
        {"role": "assistant", "content": "b"},
    ]
    replay = state.writer_replay("a b", dual_model=True)
    assert not any(m.get("role") == "tool" for m in replay.messages)
    assert replay.canonical_draft_block == ""


def test_a_single_model_lane_replays_the_trace_and_states_the_draft():
    state = TurnState(writer_content="request", resp_text="a b")
    state.writer_tool_used = True
    state.writer_trace = [
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "a ", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": "{}"},
        {"role": "assistant", "content": "b"},
    ]
    replay = state.writer_replay("a b", dual_model=False)
    assert any(m.get("role") == "tool" for m in replay.messages)
    assert "a b" in replay.canonical_draft_block


def test_the_normalized_constructor_matches_what_a_caller_would_build():
    assert WriterReplay.normalized("request", "draft").messages == (
        {"role": "user", "content": "request"},
        {"role": "assistant", "content": "draft"},
    )


# ── lane assembly ───────────────────────────────────────────────────────────


class _Client:
    completion_mode = "chat"


def _snapshot(*, binding: WriterToolBinding | None, selected: bool) -> RegistrySnapshot:
    tools = {binding.wire_name: binding} if binding is not None else {}
    return RegistrySnapshot(
        generation=1,
        workflows=MappingProxyType({}),
        by_hook=MappingProxyType({}),
        digests=MappingProxyType({}),
        fragment_types=MappingProxyType({}),
        active_writer_tool=binding.wire_name if (binding is not None and selected) else None,
        writer_tools=MappingProxyType(tools),
    )


def _config(*, snapshot, agent_client=None, settings_extra=None):
    from backend.core import Macros

    settings = {
        "model_name": "m",
        "endpoint_url": "http://writer.local/v1",
        "completion_mode": "chat",
        "enabled_tools": {},
        "agent_model_name": "am",
        **(settings_extra or {}),
    }
    return _resolve_pipeline_config(
        settings,
        {},
        macros=Macros("User", ""),
        client=_Client(),
        agent_client=agent_client,
        agent_prefix=None,
        prefix=[],
        phrase_bank=None,
        schema_overrides={},
        registry=snapshot,
    )


def test_single_model_lanes_are_the_same_object():
    cfg = _config(snapshot=_snapshot(binding=_binding(), selected=True))
    assert cfg.writer_lane is cfg.agent_lane
    assert [t["function"]["name"] for t in cfg.writer_lane.base.tools] == [WIRE]


def test_dual_model_lanes_carry_only_their_own_schemas():
    cfg = _config(
        snapshot=_snapshot(binding=_binding(), selected=True),
        agent_client=_Client(),
        settings_extra={
            "agent_same_as_writer": False,
            "agent_endpoint_id": 2,
            "agent_endpoint_url": "http://agent.local/v1",
        },
    )
    assert cfg.writer_lane is not cfg.agent_lane
    assert [t["function"]["name"] for t in cfg.writer_lane.base.tools] == [WIRE]
    assert WIRE not in [t["function"]["name"] for t in cfg.agent_lane.base.tools]


def test_an_unselected_binding_never_enters_a_blob():
    cfg = _config(snapshot=_snapshot(binding=_binding(), selected=False))
    assert cfg.writer_tool_schemas == ()
    assert cfg.writer_lane.base.tools == ()
    assert not cfg.writer_tool_policy.active


def test_agent_off_does_not_disable_an_eligible_resolver():
    """Agent enablement decides which Director/Editor tools exist. Letting it
    also decide Writer eligibility would mean turning the Director off silently
    removed a tool the user selected for the Writer."""
    cfg = _config(
        snapshot=_snapshot(binding=_binding(), selected=True),
        settings_extra={"enable_agent": False},
    )
    assert cfg.writer_tool_policy.active
    assert [t["function"]["name"] for t in cfg.writer_lane.base.tools] == [WIRE]


@pytest.mark.parametrize(
    "settings_extra",
    [{"completion_mode": "text"}],
)
def test_an_incompatible_endpoint_omits_the_schema_and_records_a_diagnostic(settings_extra):
    class TextClient:
        completion_mode = "text"

    from backend.core import Macros

    cfg = _resolve_pipeline_config(
        {
            "model_name": "m",
            "endpoint_url": "http://writer.local/v1",
            "enabled_tools": {},
            **settings_extra,
        },
        {},
        macros=Macros("User", ""),
        client=TextClient(),
        agent_client=None,
        agent_prefix=None,
        prefix=[],
        phrase_bank=None,
        schema_overrides={},
        registry=_snapshot(binding=_binding(), selected=True),
    )
    assert not cfg.writer_tool_policy.active
    assert "text-completion mode" in cfg.writer_tool_policy.diagnostic
    assert cfg.writer_lane.base.tools == ()


def test_no_snapshot_produces_the_pre_feature_configuration():
    cfg = _config(snapshot=None)
    assert cfg.writer_tool_schemas == ()
    assert cfg.writer_tool_policy == WriterToolPolicy()
