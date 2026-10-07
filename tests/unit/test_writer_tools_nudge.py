"""The writer's no-tools nudge and the gate deciding whether it is emitted.

The provider-neutral gate is whether Orb sends a non-empty schema tuple. Three plain-text configurations send nothing --
dual-model (Invariant 5), text mode, and structured endpoints. Multimodal text mode intentionally takes the chat transport, so
it is symmetric with chat mode instead. These tests pin both the frozen-base and transport halves of that decision.
"""

import pytest

from backend.core import CastMember
from backend.inference import KVCacheTracker
from backend.inference.client import LLMClient
from backend.pipeline.config import resolve_pipeline_config
from backend.pipeline.passes.writer import build_writer_content, writer_stage
from backend.pipeline.state import TurnState

NUDGE = "**Do not use tool or function calls this turn.**"

_SETTINGS = {
    "model_name": "test",
    "enable_agent": 1,
    "reasoning_enabled_passes": {},
    "length_guard_enabled": 0,
    "length_guard_enforce": 0,
    "length_guard_max_words": 240,
    "length_guard_max_paragraphs": 4,
    "workflows_globally_enabled": 1,
}

_ENABLED_TOOLS = {"direct_scene": True, "editor_apply_patch": True}


class _StubMacros:
    # Stored on the lane's CachedBase but not called during config resolution.
    def resolve_prompt_messages(self, *args, **kwargs):
        return []


def _resolve(
    client: LLMClient,
    agent_client: LLMClient | None = None,
    *,
    enabled_tools: dict[str, bool] | None = None,
    prefix: list[dict] | None = None,
    settings: dict | None = None,
):
    return resolve_pipeline_config(
        settings or _SETTINGS,
        dict(_ENABLED_TOOLS if enabled_tools is None else enabled_tools),
        macros=_StubMacros(),
        client=client,
        agent_client=agent_client,
        agent_prefix=None,
        prefix=prefix or [{"role": "system", "content": "x"}],
        phrase_bank=None,
        schema_overrides={},
    )


def _sends(cfg, content="hi") -> bool:
    return cfg.writer_lane.sends_tool_schemas([{"role": "user", "content": content}])


_IMAGE_CONTENT = [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,eA=="}}]


# -- the gate feeds the content builder ---------------------------------------


def test_nudge_emitted_only_when_tools_are_sent():
    assert NUDGE in build_writer_content("", "", True, "hi", None, None)
    assert NUDGE not in build_writer_content("", "", False, "hi", None, None)


# -- the derivation -----------------------------------------------------------


def test_ordinary_chat_endpoint_sends_tools():
    cfg = _resolve(LLMClient("http://localhost:5000/v1"))
    assert cfg.writer_lane.base.tools, "non-empty blob, or the other cases prove nothing"
    assert _sends(cfg)


def test_structured_endpoint_does_not_send_tools():
    # The blob is still built (it sources the response_format schema) but never
    # reaches the body -- so don't warn off tools the model cannot see.
    cfg = _resolve(LLMClient("https://nano-gpt.com/api/v1"))
    assert cfg.writer_lane.base.tools, "blob is still built on a structured endpoint"
    assert not _sends(cfg)


def test_text_mode_plain_call_does_not_send_tools():
    assert not _sends(_resolve(LLMClient("http://localhost:5000/v1", completion_mode="text")))


def test_text_mode_multimodal_call_sends_tools_via_chat():
    assert _sends(_resolve(LLMClient("http://localhost:5000/v1", completion_mode="text")), _IMAGE_CONTENT)


def test_text_mode_image_in_history_also_selects_chat():
    prefix = [
        {"role": "system", "content": "x"},
        {"role": "user", "content": _IMAGE_CONTENT},
        {"role": "assistant", "content": "seen"},
    ]
    assert _sends(_resolve(LLMClient("http://localhost:5000/v1", completion_mode="text"), prefix=prefix))


def test_dual_model_does_not_send_tools():
    # Invariant 5: no schemas on the writer's lane at all.
    cfg = _resolve(LLMClient("http://localhost:5000/v1"), agent_client=LLMClient("http://localhost:5001/v1"))
    assert cfg.writer_lane.base.tools == ()
    assert not _sends(cfg)


def test_false_only_enablement_map_does_not_masquerade_as_schemas():
    cfg = _resolve(
        LLMClient("http://localhost:5000/v1"),
        enabled_tools={"direct_scene": False, "editor_apply_patch": False},
        settings={**_SETTINGS, "enable_agent": 0},
    )
    assert cfg.writer_lane.base.tools == ()
    assert not _sends(cfg)


def test_agent_pass_toggles_gate_the_passes_without_rewriting_the_blob():
    client = LLMClient("http://localhost:5000/v1")
    off = _resolve(client, enabled_tools={})
    on = _resolve(client, settings={**_SETTINGS, "length_guard_enabled": 1})
    assert off.writer_lane.base.tools == on.writer_lane.base.tools
    assert [tool["function"]["name"] for tool in off.writer_lane.base.tools] == [
        "direct_scene",
        "editor_apply_patch",
        "editor_rewrite",
        "editor_search_replace",
    ]
    assert not off.active_tools.get("direct_scene") and not off.active_tools.get("editor_apply_patch")
    assert on.active_tools["direct_scene"] and on.active_tools["editor_apply_patch"]
    assert off.length_guard is None and on.length_guard is not None


@pytest.mark.parametrize("grouped", [False, True])
async def test_writer_ignores_probability_events_while_streaming_prose_and_reasoning(monkeypatch, grouped):
    client = LLMClient("http://localhost:5000/v1")
    cfg = _resolve(client)
    state = TurnState(effective_msg="hi")
    speaker = CastMember("member", "alice", "card", "Alice", "character", "", "", "", "") if grouped else None

    async def complete(**kwargs):
        yield {"type": "reasoning", "delta": "Planning."}
        yield {"type": "token_probs", "token": "Hello", "prob": 0.9, "top": []}
        if grouped:
            yield {"type": "content", "delta": "Al"}
            yield {"type": "token_probs", "token": "ice", "prob": 0.8, "top": []}
            yield {"type": "content", "delta": "ice: Hello"}
        else:
            yield {"type": "content", "delta": "Hello"}
        yield {"type": "reasoning", "delta": " Ready."}
        yield {"type": "content", "delta": " world"}
        yield {"type": "done", "message": {"content": "Hello world"}, "usage": None}

    monkeypatch.setattr(client, "complete", complete)
    events = [
        event
        async for event in writer_stage(
            cfg, state, settings=_SETTINGS, attachments=[], kv_tracker=KVCacheTracker(), speaker=speaker
        )
    ]

    assert state.resp_text == "Hello world"
    assert state.reasoning_writer == "Planning. Ready."
    assert "".join(event["data"] for event in events if event["event"] == "token") == "Hello world"
