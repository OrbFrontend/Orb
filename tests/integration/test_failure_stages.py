"""The terminal ``error`` event names the pass an internal failure escaped.

Provider failures inside the Judge and Director are skipped by those passes, so
what reaches ``stage`` here is Orb's own code failing. A group exchange runs its
Judge and Director outside ``_run_pipeline``, and must label them the same way
a solo turn does.
"""

from __future__ import annotations

import json

import pytest

from backend.pipeline import entrypoints, orchestrator
from backend.pipeline.failures import stage_of
from backend.pipeline.passes.director import director as director_module
from backend.pipeline.state import TurnState


def _error(body: str) -> dict:
    name = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[7:]
        elif line.startswith("data: ") and name == "error":
            return json.loads(line[6:])
    raise AssertionError("no error event")


async def _card(client, name: str) -> str:
    response = await client.post("/api/characters", json={"name": name})
    assert response.status_code == 200
    return response.json()["id"]


async def _conversation(client, kind: str) -> str:
    aria = await _card(client, "Aria")
    if kind == "solo":
        body: dict = {"character_card_id": aria}
    else:
        body = {"kind": "group", "group_turn_mode": "round_robin", "members": [{"character_card_id": aria}]}
    response = await client.post("/api/conversations", json=body)
    assert response.status_code == 200
    return response.json()["id"]


def _raise(*_args, **_kwargs):
    raise RuntimeError("boom")


@pytest.mark.parametrize("kind", ["solo", "group"])
async def test_an_internal_director_failure_is_labelled_the_director_pass(client, llm_mock, monkeypatch, kind):
    monkeypatch.setattr(director_module, "compute_style_injection_block", _raise)
    conv_id = await _conversation(client, kind)

    response = await client.post(f"/api/conversations/{conv_id}/send", json={"content": "Hello"})

    assert _error(response.text)["stage"] == "director pass"
    assert not any(call["pass"] == "writer" for call in llm_mock.captured)


@pytest.mark.parametrize("kind", ["solo", "group"])
async def test_an_internal_judge_failure_is_labelled_the_judge_pass(client, llm_mock, monkeypatch, kind):
    monkeypatch.setattr(entrypoints, "build_snapshot", _raise)
    conv_id = await _conversation(client, kind)

    response = await client.post(f"/api/conversations/{conv_id}/send", json={"content": "Hello"})

    assert _error(response.text)["stage"] == "judge pass"
    assert llm_mock.calls == []


async def test_a_director_prelude_failure_is_labelled_the_director_pass(monkeypatch):
    monkeypatch.setattr(orchestrator, "state_event_payload", _raise)
    state = TurnState(state_report={"rejected": [], "dropped": [{}]})

    with pytest.raises(RuntimeError) as caught:
        async for _ in orchestrator.run_director_stage(
            None,
            state,
            settings={},
            director={},
            mood_fragments=[],
            interactive_fragments=[],
            state_contract=None,
            attachments=[],
            kv_tracker=None,
            lorebook=None,
            macros=None,
        ):
            pass
    assert stage_of(caught.value) == "director pass"
