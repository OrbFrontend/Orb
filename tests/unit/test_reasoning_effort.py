"""Per-model reasoning-effort: reasoning_cfg shape, body injection, factory threading."""

import pytest

from backend.inference import EndpointConfigError
from backend.inference.client import (
    LLMClient,
    agent_client_from_settings,
    agent_lane_from_settings,
    apply_reasoning_effort,
    client_from_settings,
    reasoning_cfg,
)
from tests.http_stream import capture_wire_body as _wire_body


def _enabled_body() -> dict:
    return {"model": "m", "messages": [], "reasoning": {"enabled": True}, "thinking": {"type": "enabled"}}


def test_reasoning_cfg_on_carries_no_effort():
    cfg = reasoning_cfg(True)
    assert cfg["reasoning"] == {"enabled": True}
    assert "effort" not in cfg["reasoning"]


def test_reasoning_cfg_off_unchanged():
    cfg = reasoning_cfg(False)
    assert cfg["reasoning"] == {"effort": "none", "enabled": False}
    assert cfg["thinking"] == {"type": "disabled"}


def test_apply_level_sets_both_dialects():
    body = _enabled_body()
    shared = body["reasoning"]
    apply_reasoning_effort(body, "high")
    assert body["reasoning_effort"] == "high"
    assert body["reasoning"] == {"enabled": True, "effort": "high"}
    # The caller's reasoning dict is shared across calls; it must not be mutated.
    assert shared == {"enabled": True}


def test_apply_skips_reasoning_off_calls():
    body = {"model": "m", "reasoning": {"effort": "none", "enabled": False}}
    apply_reasoning_effort(body, "high")
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"effort": "none", "enabled": False}


def test_apply_skips_bodies_without_reasoning():
    body = {"model": "m", "messages": []}
    apply_reasoning_effort(body, "high")
    assert body == {"model": "m", "messages": []}


def test_apply_empty_effort_is_noop():
    body = _enabled_body()
    apply_reasoning_effort(body, "")
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"enabled": True}


def test_apply_custom_sends_exact_param_json_decoded():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "thinking_budget", "4096")
    assert body["thinking_budget"] == 4096
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"enabled": True}


def test_apply_custom_object_value():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "reasoning", '{"effort": "xhigh"}')
    assert body["reasoning"] == {"effort": "xhigh"}


def test_apply_custom_bare_word_stays_string():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "reasoning_effort", "max")
    assert body["reasoning_effort"] == "max"


def test_apply_custom_without_param_is_noop():
    body = _enabled_body()
    apply_reasoning_effort(body, "custom", "", "4096")
    assert body == _enabled_body()


def test_client_factory_threads_effort():
    settings = {
        "endpoint_url": "http://localhost:5000/v1",
        "reasoning_effort": "xhigh",
        "reasoning_effort_param": "p",
        "reasoning_effort_value": "v",
    }
    client = client_from_settings(settings)
    assert client.reasoning_effort == "xhigh"
    assert client.reasoning_effort_param == "p"
    assert client.reasoning_effort_value == "v"


def test_agent_factory_falls_back_to_writer_effort():
    settings = {"endpoint_url": "http://localhost:5000/v1", "reasoning_effort": "low"}
    assert agent_client_from_settings(settings).reasoning_effort == "low"


def test_agent_factory_prefers_agent_effort():
    settings = {"endpoint_url": "http://localhost:5000/v1", "reasoning_effort": "low", "agent_reasoning_effort": "high"}
    assert agent_client_from_settings(settings).reasoning_effort == "high"


def test_agent_lane_reuses_writer_client_in_single_model():
    settings = {"endpoint_url": "http://writer:5000/v1", "model_name": "writer-model", "agent_same_as_writer": True}
    writer = client_from_settings(settings)
    client, model = agent_lane_from_settings(settings, writer_client=writer)
    assert client is writer
    assert model == "writer-model"


@pytest.mark.parametrize(
    ("agent", "missing"), [({}, "no Agent endpoint is selected"), ({"agent_endpoint_id": 2}, "has no model selected")]
)
def test_a_half_configured_agent_lane_names_what_to_pick(agent, missing):
    # Turning "Same as Writer" off is the choice of a separate lane; running the Agent on the Writer instead would make it a lie.
    settings = {"endpoint_url": "http://writer:5000/v1", "model_name": "writer-model", "agent_same_as_writer": False, **agent}
    writer = client_from_settings(settings)
    with pytest.raises(EndpointConfigError, match=missing):
        agent_lane_from_settings(settings, writer_client=writer)


def test_agent_lane_uses_configured_dual_model_client():
    settings = {
        "endpoint_url": "http://writer:5000/v1",
        "model_name": "writer-model",
        "agent_same_as_writer": False,
        "agent_endpoint_id": 2,
        "agent_endpoint_url": "http://agent:6000/v1",
        "agent_model_name": "agent-model",
        "agent_reasoning_effort": "high",
    }
    writer = client_from_settings(settings)
    client, model = agent_lane_from_settings(settings, writer_client=writer)
    assert client is not writer
    assert client.base_url == "http://agent:6000/v1"
    assert client.reasoning_effort == "high"
    assert model == "agent-model"


# -- Wire-level: the client attribute must land in the outbound body ----------


async def test_wire_level_reaches_body():
    body = await _wire_body(LLMClient("http://localhost:5000/v1", reasoning_effort="high"), **reasoning_cfg(True))
    assert body["reasoning_effort"] == "high"
    assert body["reasoning"] == {"enabled": True, "effort": "high"}


async def test_wire_custom_param_reaches_body():
    client = LLMClient(
        "http://localhost:5000/v1",
        reasoning_effort="custom",
        reasoning_effort_param="thinking_budget",
        reasoning_effort_value="2048",
    )
    body = await _wire_body(client, **reasoning_cfg(True))
    assert body["thinking_budget"] == 2048
    assert "reasoning_effort" not in body


async def test_wire_reasoning_off_sends_no_effort():
    body = await _wire_body(LLMClient("http://localhost:5000/v1", reasoning_effort="high"), **reasoning_cfg(False))
    assert "reasoning_effort" not in body
    assert body["reasoning"] == {"effort": "none", "enabled": False}
