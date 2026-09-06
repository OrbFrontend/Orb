"""Each lane sends the sampler preset of the endpoint it is calling.

The Agent endpoint carries its own model config -- temperature, budget, samplers --
and the Agent passes are the ones dialing that endpoint. Sending the Writer's preset
there is the bug this file guards: it reads as the Agent ignoring its own settings.

The budget is the one key that does not pass straight through. A forced tool call
has to fit its whole answer in one reply, so the configured `max_tokens` may raise
that call's floor but never lower it.
"""

from __future__ import annotations

from typing import Any

import backend.database as dbmod
from backend.pipeline import handle_turn

# Deliberately far apart, and each key different from the other lane's, so a mixed
# spread fails on the key that leaked rather than passing on a shared default. The
# Agent budget sits above the Director's 8192 floor so it survives verbatim; the
# floored case gets its own test below.
_SAMPLERS = ("temperature", "top_k", "min_p")
_WRITER_PRESET = {"temperature": 1.15, "max_tokens": 700, "top_k": 80, "min_p": 0.02}
_AGENT_PRESET = {"temperature": 0.4, "max_tokens": 16384, "top_k": 20, "min_p": 0.1}
_DIRECTOR_FLOOR = 8192


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def _config_id(client, endpoint_id: int, role: str) -> int:
    models = (await client.get(f"/api/endpoints/{endpoint_id}/models")).json()
    return next(m["id"] for m in models if m["role"] == role)


def _params(captured: list[dict], pass_name: str) -> dict[str, Any]:
    return next(c["params"] for c in captured if c["pass"] == pass_name)


def _samplers(captured: list[dict], pass_name: str) -> dict[str, Any]:
    params = _params(captured, pass_name)
    return {k: params[k] for k in _SAMPLERS if k in params}


async def _two_lane_setup(client, agent_preset: dict) -> None:
    writer_endpoint = (await client.get("/api/endpoints")).json()[0]["id"]
    await client.put(f"/api/models/{await _config_id(client, writer_endpoint, 'writer')}", json=_WRITER_PRESET)

    # A new endpoint auto-provisions a writer and an agent model config; the agent
    # lane reads the latter once `agent_same_as_writer` is off.
    agent_endpoint = (await client.post("/api/endpoints", json={"url": "http://agent.local", "api_key": "k"})).json()["id"]
    await client.put(f"/api/models/{await _config_id(client, agent_endpoint, 'agent')}", json=agent_preset)
    await client.put(
        "/api/settings",
        json={
            "agent_same_as_writer": False,
            "agent_endpoint_id": agent_endpoint,
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True},
        },
    )


async def _run_turn(cid: str, llm_mock) -> None:
    await dbmod.create_conversation(cid, "presets", "Bot", "a scenario")
    llm_mock.enqueue_director([{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": []}}}])
    llm_mock.enqueue_writer("She nods slowly.")
    await _drain(handle_turn(cid, "hello"))


async def test_each_lane_sends_the_preset_of_the_endpoint_it_calls(client, db, llm_mock):
    await _two_lane_setup(client, _AGENT_PRESET)
    await _run_turn("conv-lane-presets", llm_mock)

    writer_params = _params(llm_mock.captured, "writer")
    assert {k: writer_params[k] for k in _WRITER_PRESET} == _WRITER_PRESET
    assert _samplers(llm_mock.captured, "director") == {k: _AGENT_PRESET[k] for k in _SAMPLERS}
    # Above the floor, so the Agent endpoint's own budget is what goes out.
    assert _params(llm_mock.captured, "director")["max_tokens"] == _AGENT_PRESET["max_tokens"]


async def test_a_short_agent_budget_never_shrinks_a_forced_call(client, db, llm_mock):
    """1024 tokens is a normal setting for an endpoint kept to brief replies.

    Honoring it for `direct_scene` truncates the tool call mid-arguments, which
    degrades to empty arguments and reaches the user as the Director silently doing
    nothing -- so the floor wins, while every sampler still comes from the Agent.
    """
    await _two_lane_setup(client, {**_AGENT_PRESET, "max_tokens": 1024})
    await _run_turn("conv-lane-floored", llm_mock)

    assert _samplers(llm_mock.captured, "director") == {k: _AGENT_PRESET[k] for k in _SAMPLERS}
    assert _params(llm_mock.captured, "director")["max_tokens"] == _DIRECTOR_FLOOR


async def test_one_endpoint_for_both_lanes_keeps_sending_its_preset(client, db, llm_mock):
    """Single-model mode has no `agent_*` overlay: the agent passes read the same
    row the writer does, because it is the same endpoint they are calling."""
    writer_endpoint = (await client.get("/api/endpoints")).json()[0]["id"]
    await client.put(f"/api/models/{await _config_id(client, writer_endpoint, 'writer')}", json=_WRITER_PRESET)
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})

    await _run_turn("conv-single-lane-presets", llm_mock)

    assert _samplers(llm_mock.captured, "director") == {k: _WRITER_PRESET[k] for k in _SAMPLERS}
    # The writer's 700-token preset is honored for prose and floored for the call.
    assert _params(llm_mock.captured, "writer")["max_tokens"] == _WRITER_PRESET["max_tokens"]
    assert _params(llm_mock.captured, "director")["max_tokens"] == _DIRECTOR_FLOOR
