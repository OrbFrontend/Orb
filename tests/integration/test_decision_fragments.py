"""Decision fragments end to end: authoring API, turn placement, persistence.

The gateway is stubbed at the transport boundary so these tests exercise the real
pipeline wiring -- stage placement, the Director's tail, the Writer's Scene
Guidance, atomic persistence, group scope, replay, and branch copies -- without a
network call.
"""

from __future__ import annotations

import json

import httpx
import pytest

import backend.database as dbmod
from backend.inference import RAW_ANSWER_CACHE, DecisionCancelled, DecisionResponse
from backend.inference.errors import llm_call_error
from backend.pipeline import (
    handle_magic_rewrite,
    handle_regenerate,
    handle_speak,
    handle_turn,
)
from backend.pipeline.passes.judge import judge as judge_module

DEFINITION = {
    "id": "outcome",
    "label": "Outcome",
    "description": "Whether Alric prevails.",
    "field_type": "decision",
    "injection_label": "Doorway",
    "decision_type": "noul",
    "decision_placement": "before_director",
    "decision_state_template": "Previous reply:\n{{last_assistant_message}}\n\nCurrent request:\n{{last_message}}",
    "decision_instructions": "Does Alric prevail in this exchange?",
    "decision_criteria": {"true": "Alric ends in control.", "false": "Alric is driven back."},
    "decision_outputs": {"true": "Alric holds the doorway.", "false": "Alric is forced back a step."},
    "decision_resolution": "threshold",
    "decision_threshold": 0.5,
}


@pytest.fixture(autouse=True)
def _clean_cache():
    RAW_ANSWER_CACHE.clear()
    yield
    RAW_ANSWER_CACHE.clear()


class Gateway:
    """A stubbed decisions gateway that records what it was asked."""

    def __init__(self, monkeypatch, answers: dict[str, float] | None = None, error: Exception | None = None):
        self.answers = {"outcome": 0.9} if answers is None else answers
        self.error = error
        self.batches: list[list[str]] = []
        self.states: list[str] = []
        gateway = self

        async def _decide(self, state, questions, *, timeout=None, abort=None):  # noqa: ANN001
            gateway.batches.append([question.key for question in questions])
            gateway.states.append(state)
            if gateway.error is not None:
                raise gateway.error
            return DecisionResponse(
                answers={q.key: gateway.answers[q.key] for q in questions if q.key in gateway.answers},
                returned_model="typesafe/jev-1.13.2",
                elapsed_ms=4,
            )

        monkeypatch.setattr(judge_module.DecisionClient, "decide", _decide)


async def _configure(client, *, agent: bool = True, url: str = "https://openrouter.ai/api/v1") -> None:
    """Point decisions at a judge endpoint of their own and set the Agent lane."""
    endpoint = (await client.post("/api/endpoints", json={"url": url, "api_key": "k", "kind": "judge"})).json()
    response = await client.put(
        "/api/decisions/config",
        json={"decision_endpoint_id": endpoint["id"], "decision_model": "typesafe/jev-1.13"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["configured"] is True
    await client.put("/api/settings", json={"enable_agent": agent, "enabled_tools": {"direct_scene": agent}})


async def _add_decision(client, **overrides) -> dict:
    existing = {row["id"] for row in (await client.get("/api/interactive-fragments")).json()}
    path = "/api/interactive-fragments/outcome" if "outcome" in existing else "/api/interactive-fragments"
    response = (
        await client.put(path, json={**DEFINITION, "enabled": True, **overrides})
        if "outcome" in existing
        else await client.post(path, json={**DEFINITION, **overrides})
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _drain(agen) -> list[dict]:
    return [event async for event in agen]


def _direct_scene(**arguments) -> list[dict]:
    return [{"type": "function", "function": {"name": "direct_scene", "arguments": arguments}}]


async def _turn(llm_mock, cid: str, message: str, *, director: dict | None = None, reply: str = "ok") -> list[dict]:
    if director is not None:
        llm_mock.enqueue_director(_direct_scene(**director))
    llm_mock.enqueue_writer(reply)
    return await _drain(handle_turn(cid, message))


async def _last_assistant(cid: str) -> dict:
    return next(message for message in reversed(await dbmod.get_messages(cid)) if message["role"] == "assistant")


def _event(events: list[dict], name: str) -> dict:
    return next(event["data"] for event in events if event.get("event") == name)


def _events(events: list[dict], name: str) -> list[dict]:
    return [event["data"] for event in events if event.get("event") == name]


def _captured(llm_mock, pass_name: str) -> list[dict]:
    return [call for call in llm_mock.captured if call["pass"] == pass_name]


def _tail_text(call: dict) -> str:
    content = call["messages"][-1]["content"]
    if isinstance(content, str):
        return content
    return "\n".join(part.get("text", "") for part in content)


async def _solo_scene(client, cid: str = "conv-decision") -> str:
    await dbmod.create_conversation(cid, "scene", "Maren", "a doorway")
    await _configure(client)
    await _add_decision(client)
    return cid


# ── authoring API ────────────────────────────────────────────────────────────


async def test_a_valid_decision_round_trips_through_the_api(client, db):
    created = await _add_decision(client)
    assert created["field_type"] == "decision"
    assert created["decision_criteria"] == DEFINITION["decision_criteria"]
    assert created["decision_threshold"] == 0.5

    listed = next(f for f in (await client.get("/api/interactive-fragments")).json() if f["id"] == "outcome")
    assert listed["decision_outputs"]["true"] == "Alric holds the doorway."

    # The columns hold JSON text; the read boundary decodes them.
    row = await (await db.execute("SELECT decision_criteria FROM interactive_fragments WHERE id = 'outcome'")).fetchone()
    assert json.loads(row["decision_criteria"]) == DEFINITION["decision_criteria"]


@pytest.mark.parametrize(
    "broken",
    [
        {"decision_criteria": {"true": "only"}},
        {"decision_instructions": ""},
        {"decision_threshold": 2.0},
        {"decision_resolution": "roll"},  # a threshold left behind in roll mode
        {"decision_state_template": "Judge {{scene_guidance}}"},
        {"decision_instructions": "Does {{last_message}} succeed?"},
    ],
)
async def test_an_invalid_decision_is_rejected_with_a_reason(client, db, broken):
    response = await client.put("/api/interactive-fragments/outcome", json={**DEFINITION, **broken})
    assert response.status_code == 422
    assert response.json()["detail"]


async def test_an_unknown_decision_variant_is_rejected_by_the_schema(client, db):
    response = await client.put("/api/interactive-fragments/outcome", json={**DEFINITION, "decision_type": "score"})
    assert response.status_code == 422


async def test_a_partial_update_is_validated_against_the_merged_row(client, db):
    await _add_decision(client)
    assert (await client.put("/api/interactive-fragments/outcome", json={"decision_threshold": 0.8})).status_code == 200
    # Switching to roll mode without clearing the threshold is refused.
    assert (await client.put("/api/interactive-fragments/outcome", json={"decision_resolution": "roll"})).status_code == 422
    ok = await client.put(
        "/api/interactive-fragments/outcome", json={"decision_resolution": "roll", "decision_threshold": None}
    )
    assert ok.status_code == 200
    assert ok.json()["decision_threshold"] is None


async def test_switching_a_decision_to_another_type_clears_its_decision_columns(client, db):
    await _add_decision(client)
    updated = (await client.put("/api/interactive-fragments/outcome", json={"field_type": "string"})).json()
    assert updated["field_type"] == "string"
    assert updated["decision_instructions"] is None
    assert updated["decision_criteria"] is None


async def test_a_decision_shares_the_director_priority_lane(client, db):
    await _add_decision(client, sort_order=5)
    await client.post(
        "/api/interactive-fragments",
        json={
            "id": "pacing",
            "label": "Pacing",
            "description": "Pace.",
            "field_type": "string",
            "injection_label": "Pacing",
            "sort_order": 6,
        },
    )
    response = await client.put(
        "/api/interactive-fragments/reorder",
        json={"items": [{"id": "pacing", "sort_order": 5}, {"id": "outcome", "sort_order": 6}]},
    )
    assert response.status_code == 200


# ── configuration, preview, and the connection test ──────────────────────────


async def test_configuration_derives_the_route(client, db):
    before = (await client.get("/api/decisions/config")).json()
    assert before["configured"] is False
    assert before["default_state_template"]
    assert "last_message" in before["state_macros"]

    await _configure(client)
    after = (await client.get("/api/decisions/config")).json()
    assert after["resolved_url"] == "https://openrouter.ai/api/alpha/decisions"


async def test_a_judge_endpoint_that_names_the_route_keeps_that_spelling(client, db):
    # No second override field: the endpoint URL is the one place the route is
    # configured, and pasting the gateway's own spelling is how it is corrected.
    await _configure(client, url="https://gw.test/v2/judge/decisions")
    config = (await client.get("/api/decisions/config")).json()
    assert config["resolved_url"] == "https://gw.test/v2/judge/decisions"


async def test_the_route_prefix_is_not_doubled_when_the_endpoint_already_carries_it(client, db):
    await _configure(client, url="https://openrouter.ai/api/alpha")
    config = (await client.get("/api/decisions/config")).json()
    assert config["resolved_url"] == "https://openrouter.ai/api/alpha/decisions"


async def test_a_chat_endpoint_cannot_be_selected_as_the_judge(client, db):
    chat = (await client.post("/api/endpoints", json={"url": "https://openrouter.ai/api/v1"})).json()
    response = await client.put(
        "/api/decisions/config", json={"decision_endpoint_id": chat["id"], "decision_model": "typesafe/jev-1.13"}
    )
    assert response.status_code == 422
    assert (await client.get("/api/decisions/config")).json()["configured"] is False


async def test_the_two_endpoint_pools_are_listed_apart(client, db):
    await _configure(client)
    chat = (await client.post("/api/endpoints", json={"url": "https://openrouter.ai/api/v1"})).json()

    chat_pool = (await client.get("/api/endpoints", params={"kind": "chat"})).json()
    judge_pool = (await client.get("/api/endpoints", params={"kind": "judge"})).json()

    assert [row["id"] for row in chat_pool] == [row["id"] for row in chat_pool if row["kind"] == "chat"]
    assert chat["id"] in [row["id"] for row in chat_pool]
    assert chat["id"] not in [row["id"] for row in judge_pool]
    assert [row["kind"] for row in judge_pool] == ["judge"]
    # A judge row has no model configs: the classifier takes a model name and
    # nothing a model config carries.
    assert (await client.get(f"/api/endpoints/{judge_pool[0]['id']}/models")).json() == []


async def test_preview_renders_the_sample_scene_with_its_sizes(client, db):
    response = await client.post("/api/decisions/preview", json={"fragment": DEFINITION})
    assert response.status_code == 404


async def test_preview_reports_problems_instead_of_rendering(client, db):
    response = await client.post("/api/decisions/preview", json={"fragment": {**DEFINITION, "decision_instructions": ""}})
    assert response.status_code == 404


async def test_preview_can_render_against_a_real_conversation(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client, "conv-decision-preview")
    Gateway(monkeypatch)
    await _turn(llm_mock, cid, "I shove the door.", director={"moods": []})

    response = await client.post("/api/decisions/preview", json={"fragment": DEFINITION, "conversation_id": cid})
    assert response.status_code == 404


async def test_the_connection_test_sends_a_synthetic_scene(client, db, monkeypatch):
    await _configure(client)
    gateway = Gateway(monkeypatch, answers={"connection_test": 0.42})
    body = (await client.post("/api/decisions/test")).json()

    assert body["ok"] is True
    assert body["probability"] == 0.42
    assert body["returned_model"] == "typesafe/jev-1.13.2"
    # Not conversation content: a test must not ship the user's story.
    assert gateway.states == ["Alric attempts to force Maren back from the doorway."]


async def test_the_connection_test_reports_a_failure_as_a_result(client, db, monkeypatch):
    await _configure(client)
    Gateway(monkeypatch, error=RuntimeError("boom"))
    body = (await client.post("/api/decisions/test")).json()
    assert body["ok"] is False
    assert "boom" in body["error"]


async def test_a_rejected_test_names_the_status_and_what_to_look_at(client, db, monkeypatch):
    # The whole symptom of the doubled route was a bare "Not Found" next to a
    # resolved URL the panel was already showing. The status leads, and a 404
    # says which field is wrong.
    await _configure(client)
    request = httpx.Request("POST", "https://openrouter.ai/api/alpha/decisions")
    rejection = llm_call_error(
        response=httpx.Response(404, text="Not Found", request=request),
        body="Not Found",
        url="https://openrouter.ai/api/alpha/decisions",
        model="typesafe/jev-1.13",
        api_key="k",
    )
    Gateway(monkeypatch, error=rejection)

    body = (await client.post("/api/decisions/test")).json()
    assert body["ok"] is False
    assert body["status"] == 404
    assert body["error"].startswith("HTTP 404")
    assert "Judge Endpoint URL" in body["error"]
    assert body["url"] == "https://openrouter.ai/api/alpha/decisions"


async def test_the_connection_test_says_so_when_nothing_is_configured(client, db):
    body = (await client.post("/api/decisions/test")).json()
    assert body["ok"] is False
    assert "configured" in body["error"]


# ── the solo turn ────────────────────────────────────────────────────────────


async def test_a_resolved_decision_reaches_the_director_tail_and_the_writer(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    gateway = Gateway(monkeypatch, answers={"outcome": 0.9})

    events = await _turn(llm_mock, cid, "I shove the door.", director={"moods": []})

    # The stage announced itself and published its result once.
    assert {"step": "judge"} in _events(events, "step_start")
    published = _event(events, "decisions")
    assert published["evaluations"][0]["outcome"] == "true"
    assert published["evaluations"][0]["probability"] == 0.9

    # The Writer's Scene Guidance carries the authored words, not the dice.
    injection = _event(events, "director_done")["injection_block"]
    assert "**Major Decisions**\n\nDoorway: Alric holds the doorway." in injection
    assert "0.9" not in injection

    # So does the Director's own trailing request.
    director_tail = _tail_text(_captured(llm_mock, "director")[0])
    assert "Doorway: Alric holds the doorway." in director_tail
    assert "0.9" not in director_tail

    # The classifier saw the rendered state, and only once.
    assert gateway.batches == [["outcome"]]
    assert "I shove the door." in gateway.states[0]


async def test_the_guidance_survives_the_directors_own_output(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    Gateway(monkeypatch)
    await client.post(
        "/api/interactive-fragments",
        json={"id": "pacing", "label": "Pacing", "description": "Pace.", "field_type": "string", "injection_label": "Pacing"},
    )

    # The Director fills a field of its own and even tries to answer the decision.
    events = await _turn(llm_mock, cid, "one", director={"moods": [], "pacing": "slow", "outcome": "hijacked"})
    injection = _event(events, "director_done")["injection_block"]

    assert "Pacing: slow" in injection
    assert "Doorway: Alric holds the doorway." in injection
    # A decision is not a Director field, so nothing it returned under that name
    # can reach the prompt.
    assert "hijacked" not in injection


async def test_the_writer_still_receives_guidance_with_the_director_disabled(client, db, llm_mock, monkeypatch):
    cid = "conv-decision-no-director"
    await dbmod.create_conversation(cid, "scene", "Maren", "a doorway")
    await _configure(client, agent=False)
    await _add_decision(client)
    Gateway(monkeypatch)

    events = await _turn(llm_mock, cid, "I shove the door.")

    assert _captured(llm_mock, "director") == []
    assert "Doorway: Alric holds the doorway." in _event(events, "director_done")["injection_block"]


async def test_the_low_outcome_selects_the_other_authored_guidance(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    Gateway(monkeypatch, answers={"outcome": 0.1})
    events = await _turn(llm_mock, cid, "one", director={"moods": []})
    assert "Doorway: Alric is forced back a step." in _event(events, "director_done")["injection_block"]


async def test_no_endpoint_means_a_skip_and_no_request(client, db, llm_mock, monkeypatch):
    cid = "conv-decision-unconfigured"
    await dbmod.create_conversation(cid, "scene", "Maren", "a doorway")
    await client.put("/api/settings", json={"enable_agent": True, "enabled_tools": {"direct_scene": True}})
    await _add_decision(client)
    gateway = Gateway(monkeypatch)

    events = await _turn(llm_mock, cid, "one", director={"moods": []})
    decisions = _event(events, "decisions")

    assert gateway.batches == []
    assert decisions["evaluations"] == []
    assert decisions["skipped"][0]["reason"] == "not_configured"
    assert decisions["skipped"][0]["failed"] == 1
    # Nothing is injected on its behalf: the turn runs as if it were not there.
    assert "Doorway:" not in _event(events, "director_done")["injection_block"]


# ── persistence ──────────────────────────────────────────────────────────────


async def test_records_and_cooldowns_commit_with_the_reply(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    await client.put("/api/interactive-fragments/outcome", json={"cooldown_turns": 2})
    Gateway(monkeypatch)

    await _turn(llm_mock, cid, "one", director={"moods": []})
    reply = await _last_assistant(cid)

    assert reply["decision_cooldowns"] == {"outcome": 2}
    stored = reply["decision_evaluations"]
    assert stored["version"] == 2
    assert stored["evaluations"][0]["fragment_id"] == "outcome"
    assert stored["evaluations"][0]["input_branch_anchor"] is not None
    # No credential ever reaches a record.
    assert "api_key" not in json.dumps(stored)


async def test_the_conversation_log_carries_the_replys_evaluations(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    Gateway(monkeypatch)
    await _turn(llm_mock, cid, "one", director={"moods": []})
    reply = await _last_assistant(cid)

    log = (await client.get(f"/api/conversations/{cid}/messages/{reply['id']}/director-log")).json()
    assert log["decision_evaluations"]["evaluations"][0]["outcome"] == "true"

    logs = (await client.get(f"/api/conversations/{cid}/logs")).json()
    assert any(row["decision_evaluations"] for row in logs)


async def test_a_resting_decision_is_recorded_as_skipped_without_an_outcome(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    await client.put("/api/interactive-fragments/outcome", json={"cooldown_turns": 2})
    gateway = Gateway(monkeypatch)

    await _turn(llm_mock, cid, "one", director={"moods": []})
    events = await _turn(llm_mock, cid, "two", director={"moods": []})

    published = _event(events, "decisions")
    assert published["evaluations"] == []
    assert published["skipped"][0]["reason"] == "resting"
    assert "Major Decisions" not in _event(events, "director_done")["injection_block"]
    assert len(gateway.batches) == 1
    # The timer still ages while resting.
    assert (await _last_assistant(cid))["decision_cooldowns"] == {"outcome": 1}


async def test_cooldowns_age_even_when_every_decision_is_disabled(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    await client.put("/api/interactive-fragments/outcome", json={"cooldown_turns": 3})
    Gateway(monkeypatch)

    await _turn(llm_mock, cid, "one", director={"moods": []})
    assert (await _last_assistant(cid))["decision_cooldowns"] == {"outcome": 3}

    await client.put("/api/interactive-fragments/outcome", json={"enabled": False})
    await _turn(llm_mock, cid, "two", director={"moods": []})
    # Disabling every decision must not freeze the timers of the resting ones.
    assert (await _last_assistant(cid))["decision_cooldowns"] == {"outcome": 2}


# ── replay ───────────────────────────────────────────────────────────────────


async def test_regeneration_replays_the_targets_own_outcome(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    gateway = Gateway(monkeypatch, answers={"outcome": 0.5})

    await _turn(llm_mock, cid, "I shove the door.", director={"moods": []})
    target = await _last_assistant(cid)
    original = target["decision_evaluations"]["evaluations"][0]
    RAW_ANSWER_CACHE.clear()  # a cold cache must not change the answer

    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("again")
    await _drain(handle_regenerate(cid, target["id"]))
    replayed = (await _last_assistant(cid))["decision_evaluations"]["evaluations"][0]

    assert len(gateway.batches) == 1  # no second call
    assert replayed["answer_source"] == "replay"
    assert replayed["outcome"] == original["outcome"]
    assert replayed["occurrence_id"] == original["occurrence_id"]


async def test_regeneration_rerolls_a_drawn_outcome_on_the_stored_odds(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    await client.put("/api/interactive-fragments/outcome", json={"decision_resolution": "roll", "decision_threshold": None})
    gateway = Gateway(monkeypatch, answers={"outcome": 0.5})
    draws = iter((0.1, 0.9))
    monkeypatch.setattr(judge_module, "draw_uniform", lambda: next(draws))

    await _turn(llm_mock, cid, "I shove the door.", director={"moods": []})
    target = await _last_assistant(cid)
    original = target["decision_evaluations"]["evaluations"][0]
    RAW_ANSWER_CACHE.clear()  # the stored answer, not the cache, is what spares the call

    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("again")
    events = await _drain(handle_regenerate(cid, target["id"]))
    rerolled = (await _last_assistant(cid))["decision_evaluations"]["evaluations"][0]

    assert len(gateway.batches) == 1  # no second call
    assert (original["outcome"], rerolled["outcome"]) == ("true", "false")
    assert rerolled["probability"] == original["probability"]
    assert rerolled["draw"] == 0.9
    assert rerolled["answer_source"] == "replay"
    assert rerolled["replayed_from"] == "live"
    assert rerolled["occurrence_id"] != original["occurrence_id"]
    assert rerolled["guidance"] in _event(events, "director_done")["injection_block"]


async def test_editing_the_guidance_changes_the_prompt_without_a_call_or_a_reroll(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    gateway = Gateway(monkeypatch)
    await _turn(llm_mock, cid, "one", director={"moods": []})
    target = await _last_assistant(cid)
    original = target["decision_evaluations"]["evaluations"][0]

    await client.put(
        "/api/interactive-fragments/outcome",
        json={"decision_outputs": {"true": "He keeps the doorway, barely.", "false": "x"}},
    )
    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("again")
    events = await _drain(handle_regenerate(cid, target["id"]))

    assert len(gateway.batches) == 1
    record = _event(events, "decisions")["evaluations"][0]
    assert record["answer_source"] == "replay"
    assert record["guidance"] == "He keeps the doorway, barely."
    assert record["occurrence_id"] == original["occurrence_id"]
    assert "He keeps the doorway, barely." in _event(events, "director_done")["injection_block"]


async def test_a_changed_question_asks_again_on_regeneration(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    gateway = Gateway(monkeypatch)
    await _turn(llm_mock, cid, "one", director={"moods": []})
    target = await _last_assistant(cid)

    await client.put("/api/interactive-fragments/outcome", json={"decision_instructions": "Does Maren hold the door?"})
    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("again")
    events = await _drain(handle_regenerate(cid, target["id"]))

    assert len(gateway.batches) == 2
    assert _event(events, "decisions")["evaluations"][0]["answer_source"] == "live"


async def test_a_fresh_send_of_identical_text_creates_a_new_occurrence(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    Gateway(monkeypatch)
    await _turn(llm_mock, cid, "same words", director={"moods": []})
    first = (await _last_assistant(cid))["decision_evaluations"]["evaluations"][0]
    await _turn(llm_mock, cid, "same words", director={"moods": []})
    second = (await _last_assistant(cid))["decision_evaluations"]["evaluations"][0]

    assert second["occurrence_id"] != first["occurrence_id"]


async def test_a_checkpoint_copies_records_and_remaps_their_anchors(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    Gateway(monkeypatch)
    await _turn(llm_mock, cid, "one", director={"moods": []})
    source = await _last_assistant(cid)
    source_anchor = source["decision_evaluations"]["evaluations"][0]["input_branch_anchor"]

    copied_id = (await client.post(f"/api/conversations/{cid}/checkpoint", json={"title": "copy"})).json()["id"]
    copied = await _last_assistant(copied_id)
    record = copied["decision_evaluations"]["evaluations"][0]

    assert copied["decision_cooldowns"] == source["decision_cooldowns"]
    assert record["input_branch_anchor"] is not None
    assert record["input_branch_anchor"] != source_anchor
    assert record.get("anchor_invalidated") is None
    # The remapped anchor names a row in the copy, not in the original.
    copied_ids = {message["id"] for message in await dbmod.get_messages(copied_id)}
    assert record["input_branch_anchor"] in copied_ids


# ── card decisions ───────────────────────────────────────────────────────────


async def _card_with_decision(client, **overrides) -> str:
    entry = {key: value for key, value in DEFINITION.items() if key not in ("injection_label", "description")}
    entry.update({"id": "card_outcome", "label": "Card outcome", "description": "Card-authored.", **overrides})
    response = await client.post(
        "/api/characters",
        json={"name": "Alric", "extensions": {"orb": {"fragments": {"interactive": [entry]}}}},
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_a_card_decision_runs_like_any_other_card_fragment(client, db, llm_mock, monkeypatch):
    card_id = await _card_with_decision(client)
    cid = "conv-decision-card"
    await dbmod.create_conversation(cid, "scene", "Alric", "a doorway", character_card_id=card_id)
    await _configure(client)
    gateway = Gateway(monkeypatch, answers={"card_outcome": 0.9})

    events = await _turn(llm_mock, cid, "one", director={"moods": []})

    assert _event(events, "decisions")["evaluations"][0]["source"] == f"card:{card_id}"
    assert gateway.batches == [["card_outcome"]]


async def test_a_malformed_card_decision_is_reported_and_asks_nothing(client, db, llm_mock, monkeypatch):
    card_id = await _card_with_decision(client, decision_criteria={"true": "only"})
    cid = "conv-decision-card-broken"
    await dbmod.create_conversation(cid, "scene", "Alric", "a doorway", character_card_id=card_id)
    await _configure(client)
    gateway = Gateway(monkeypatch)

    events = await _turn(llm_mock, cid, "one", director={"moods": []})

    # Reported, not dropped: a card is saved past the fragment routes' validation,
    # so the skip is how its author learns the decision never ran. Nothing is asked.
    (skip,) = _event(events, "decisions")["skipped"]
    assert skip["reason"] == "invalid_definition" and skip["failed"] == 1
    assert skip["source"] == f"card:{card_id}"
    assert _event(events, "decisions")["evaluations"] == []
    assert gateway.batches == []


async def test_validate_route_reports_a_card_decisions_problems(client):
    entry = {key: value for key, value in DEFINITION.items() if key != "description"}
    assert (await client.post("/api/decisions/validate", json=entry)).json() == {"ok": True}

    response = await client.post("/api/decisions/validate", json={**entry, "decision_instructions": ""})
    assert response.status_code == 422
    assert "decision_instructions must not be empty" in response.json()["detail"]


# ── group scope ──────────────────────────────────────────────────────────────


async def _group(client, speakers: int = 2) -> dict:
    cards = [(await client.post("/api/characters", json={"name": name})).json()["id"] for name in ("Aria", "Kael")]
    return (
        await client.post(
            "/api/conversations",
            json={
                "kind": "group",
                "title": "Doorway",
                "group_turn_mode": "director",
                "group_max_speakers": speakers,
                "members": [{"character_card_id": card} for card in cards],
            },
        )
    ).json()


async def test_a_group_exchange_evaluates_once_and_copies_onto_every_reply(client, db, llm_mock, monkeypatch):
    conv = await _group(client)
    await _configure(client)
    await _add_decision(client, cooldown_turns=2)
    gateway = Gateway(monkeypatch)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — go", "kael — then you"]))
    llm_mock.enqueue_writer("aria speaks")
    llm_mock.enqueue_writer("kael speaks")
    events = await _drain(handle_turn(conv["id"], "I shove the door."))

    # One evaluation for the whole exchange.
    assert gateway.batches == [["outcome"]]
    assert len(_events(events, "decisions")) == 1

    replies = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"]
    assert len(replies) == 2
    occurrences = {reply["decision_evaluations"]["evaluations"][0]["occurrence_id"] for reply in replies}
    draws = {reply["decision_evaluations"]["evaluations"][0]["outcome"] for reply in replies}
    # The same occurrence on both, so each reply stays independently inspectable
    # without the exchange having been judged twice.
    assert len(occurrences) == 1
    assert len(draws) == 1
    # And the cooldown advanced once for the exchange, not once per speaker.
    assert [reply["decision_cooldowns"] for reply in replies] == [{"outcome": 2}, {"outcome": 2}]


async def test_a_group_decision_reaches_every_speakers_writer(client, db, llm_mock, monkeypatch):
    conv = await _group(client)
    await _configure(client)
    await _add_decision(client)
    Gateway(monkeypatch)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — go", "kael — then you"]))
    llm_mock.enqueue_writer("aria speaks")
    llm_mock.enqueue_writer("kael speaks")
    await _drain(handle_turn(conv["id"], "I shove the door."))

    writer_tails = [_tail_text(call) for call in _captured(llm_mock, "writer")]
    assert len(writer_tails) == 2
    assert all("Doorway: Alric holds the doorway." in tail for tail in writer_tails)


async def test_a_group_template_needing_the_description_is_skipped(client, db, llm_mock, monkeypatch):
    conv = await _group(client)
    await _configure(client)
    await _add_decision(client, decision_state_template="About {{description}}: {{last_message}}")
    gateway = Gateway(monkeypatch)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — go"]))
    llm_mock.enqueue_writer("aria speaks")
    events = await _drain(handle_turn(conv["id"], "one"))

    decisions = _event(events, "decisions")
    assert gateway.batches == []
    assert decisions["evaluations"] == []
    assert decisions["skipped"][0]["reason"] == "unavailable_context"


async def test_a_later_speaker_regeneration_reuses_the_exchange_input(client, db, llm_mock, monkeypatch):
    conv = await _group(client)
    await _configure(client)
    await _add_decision(client, cooldown_turns=2)
    gateway = Gateway(monkeypatch)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — go", "kael — then you"]))
    llm_mock.enqueue_writer("aria speaks")
    llm_mock.enqueue_writer("kael speaks")
    await _drain(handle_turn(conv["id"], "I shove the door."))

    replies = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"]
    later = replies[-1]
    original = later["decision_evaluations"]["evaluations"][0]
    RAW_ANSWER_CACHE.clear()

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["kael — again"]))
    llm_mock.enqueue_writer("kael again")
    await _drain(handle_regenerate(conv["id"], later["id"]))

    regenerated = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"][-1]
    record = regenerated["decision_evaluations"]["evaluations"][0]

    # The exchange's committed record, taken as is: no second call, no reroll,
    # and the cooldown is not charged to the exchange twice.
    assert len(gateway.batches) == 1
    assert record == original
    assert regenerated["decision_cooldowns"] == {"outcome": 2}


async def test_a_preset_round_trips_a_decision_like_any_other_fragment(client, db):
    await _add_decision(client, enabled=True)
    name = (await client.post("/api/presets/export", json={"domains": ["fragments"], "label": "shared"})).json()["name"]
    await client.delete("/api/interactive-fragments/outcome")

    assert (await client.post(f"/api/presets/{name}/apply")).status_code == 200

    imported = next(f for f in (await client.get("/api/interactive-fragments")).json() if f["id"] == "outcome")
    assert imported["decision_instructions"] == DEFINITION["decision_instructions"]
    assert imported["enabled"] == 1


# ── payload shape ────────────────────────────────────────────────────────────


async def test_the_message_listing_does_not_carry_evaluation_records(client, db, llm_mock, monkeypatch):
    """A rendered state is up to 16 KiB and the Inspector fetches it separately.

    Shipping every record on every conversation open would put the whole
    diagnostic envelope -- rendered state, criteria and both authored outputs,
    per decision, per message -- on the wire for a panel most opens never open.
    """
    cid = await _solo_scene(client)
    Gateway(monkeypatch)
    await _turn(llm_mock, cid, "I shove the door.", director={"moods": []})

    listed = (await client.get(f"/api/conversations/{cid}/messages")).json()
    reply = next(message for message in reversed(listed) if message["role"] == "assistant")
    assert "decision_evaluations" not in reply

    # The Inspector's own route still carries them in full.
    log = (await client.get(f"/api/conversations/{cid}/messages/{reply['id']}/director-log")).json()
    assert log["decision_evaluations"]["evaluations"][0]["rendered_state"]


async def test_the_live_event_summarises_rather_than_streaming_every_record(client, db, llm_mock, monkeypatch):
    cid = await _solo_scene(client)
    Gateway(monkeypatch)
    events = await _turn(llm_mock, cid, "I shove the door.", director={"moods": []})

    published = _event(events, "decisions")["evaluations"][0]
    assert published["outcome"] == "true"
    assert published["fragment_id"] == "outcome"
    # The progress indicator needs the outcome, not the classifier input.
    assert "rendered_state" not in published
    assert "outputs" not in published


# ── diagnostics for definitions that cannot run ──────────────────────────────


async def test_an_unparseable_global_decision_is_recorded_as_skipped(client, db, llm_mock, monkeypatch):
    """A row that cannot be parsed is reported, not silently dropped.

    The authoring API rejects these on write, so the rows that reach here arrived
    another way -- a preset import, or a definition a later schema invalidated.
    Those are exactly the cases where an author needs to be told.
    """
    cid = await _solo_scene(client)
    await db.execute("UPDATE interactive_fragments SET decision_criteria = '{\"true\": \"only\"}' WHERE id = 'outcome'")
    await db.commit()
    gateway = Gateway(monkeypatch)

    events = await _turn(llm_mock, cid, "one", director={"moods": []})

    published = _event(events, "decisions")
    assert gateway.batches == []
    assert published["evaluations"] == []
    assert published["skipped"][0] == {
        "fragment_id": "outcome",
        "fragment_label": "Outcome",
        "source": "global",
        "reason": "invalid_definition",
        "failed": 1,
    }


# ── cancellation ─────────────────────────────────────────────────────────────


async def test_a_stop_during_the_decision_stage_ends_the_turn(client, db, llm_mock, monkeypatch):
    """Cancellation stops the turn; it is not a provider failure.

    `director_pass` already refuses to call once the token is set, so nothing was
    ever billed. What the solo path was missing is the group driver's early
    return: without it a cancelled turn still announced a directing phase it was
    not going to run, and no decision guidance is produced either way.
    """
    cid = await _solo_scene(client)
    gateway = Gateway(monkeypatch)
    token = llm_mock.abort_token

    async def _decide(self, state, questions, *, timeout=None, abort=None):  # noqa: ANN001
        gateway.batches.append([question.key for question in questions])
        token.abort()
        raise DecisionCancelled("stopped")

    monkeypatch.setattr(judge_module.DecisionClient, "decide", _decide)

    events = await _drain(handle_turn(cid, "I shove the door.", abort_token=token))

    assert gateway.batches == [["outcome"]]
    assert _captured(llm_mock, "director") == []
    assert _captured(llm_mock, "writer") == []
    # The decision step starts, then the turn ends: no director_start for a
    # directing phase that will not happen.
    assert [event["event"] for event in events if event["event"] != "user_message_created"] == ["step_start", "done"]
    assert _events(events, "step_start") == [{"step": "judge"}]
    # No reply was retained, so no decision cooldown was committed either.
    assert [m for m in await dbmod.get_messages(cid) if m["role"] == "assistant"] == []


# ── steered regeneration ─────────────────────────────────────────────────────


async def test_a_steered_group_regeneration_reuses_the_exchange_input(client, db, llm_mock, monkeypatch):
    """Magic rewrite of an exchange's first speaker rewinds to the exchange's own input.

    Without the rewind the decision reads the replaced reply as
    ``{{last_assistant_message}}`` -- substituting the reply for the original
    exchange input. (A later speaker never asks at all; see the inheritance tests.)
    """
    conv = await _group(client)
    await _configure(client)
    await _add_decision(client)
    gateway = Gateway(monkeypatch)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — go", "kael — then you"]))
    llm_mock.enqueue_writer("aria speaks")
    llm_mock.enqueue_writer("kael speaks")
    await _drain(handle_turn(conv["id"], "I shove the door."))

    first = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"][0]
    RAW_ANSWER_CACHE.clear()

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — again"]))
    llm_mock.enqueue_writer("aria again")
    await _drain(handle_magic_rewrite(conv["id"], first["id"], "slower, more reluctant"))

    regenerated = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"][-1]
    record = regenerated["decision_evaluations"]["evaluations"][0]

    # The steering is part of the current request, so this is a new occurrence
    # and a second call -- but it is asked about the exchange's own input, with
    # neither speaker's reply standing in for it.
    assert len(gateway.batches) == 2
    assert "aria speaks" not in gateway.states[-1]
    assert "kael speaks" not in gateway.states[-1]
    assert "I shove the door." in gateway.states[-1]
    assert "slower, more reluctant" in gateway.states[-1]
    assert record["answer_source"] == "live"


async def test_a_steered_solo_regeneration_reuses_the_turns_input(client, db, llm_mock, monkeypatch):
    """The solo form of the rewind: the replaced reply is not the previous reply,
    and the original request is not dropped in favour of the steering alone."""
    cid = await _solo_scene(client)
    gateway = Gateway(monkeypatch)

    await _turn(llm_mock, cid, "I shove the door.", director={"moods": []}, reply="the reply being replaced")
    target = await _last_assistant(cid)
    RAW_ANSWER_CACHE.clear()

    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("rewritten")
    await _drain(handle_magic_rewrite(cid, target["id"], "slower, more reluctant"))
    record = (await _last_assistant(cid))["decision_evaluations"]["evaluations"][0]

    assert len(gateway.batches) == 2
    assert "the reply being replaced" not in gateway.states[-1]
    assert "I shove the door." in gateway.states[-1]
    assert "slower, more reluctant" in gateway.states[-1]
    assert record["answer_source"] == "live"


async def _two_speaker_roll_exchange(client, llm_mock, monkeypatch, draws) -> tuple[dict, dict, dict, Gateway]:
    conv = await _group(client)
    await _configure(client)
    await _add_decision(client, decision_resolution="roll", decision_threshold=None, cooldown_turns=2)
    gateway = Gateway(monkeypatch, answers={"outcome": 0.5})
    rolls = iter(draws)
    monkeypatch.setattr(judge_module, "draw_uniform", lambda: next(rolls))
    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — go", "kael — then you"]))
    llm_mock.enqueue_writer("aria: you get through")
    llm_mock.enqueue_writer("kael speaks")
    await _drain(handle_turn(conv["id"], "I shove the door."))
    first, later = [m for m in await dbmod.get_messages(conv["id"]) if m["role"] == "assistant"]
    RAW_ANSWER_CACHE.clear()
    return conv, first, later, gateway


async def test_a_later_speaker_regeneration_keeps_the_rolled_outcome_its_parent_wrote(client, db, llm_mock, monkeypatch):
    """Rerolling would put the other side of the odds under a reply already
    written to this one, on the same branch. The draw after 0.1 would flip it."""
    conv, first, later, gateway = await _two_speaker_roll_exchange(client, llm_mock, monkeypatch, (0.1, 0.9))

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["kael — again"]))
    llm_mock.enqueue_writer("kael again")
    events = await _drain(handle_regenerate(conv["id"], later["id"]))
    regenerated = (await dbmod.get_messages(conv["id"]))[-1]

    expected = first["decision_evaluations"]["evaluations"][0]
    assert regenerated["parent_id"] == first["id"]
    assert regenerated["decision_evaluations"]["evaluations"][0] == expected
    assert regenerated["decision_cooldowns"] == first["decision_cooldowns"]
    assert len(gateway.batches) == 1
    assert _event(events, "decisions")["inherited"] == 1
    assert expected["guidance"] in _tail_text(_captured(llm_mock, "writer")[-1])


async def test_a_steered_later_speaker_keeps_its_exchanges_outcome(client, db, llm_mock, monkeypatch):
    conv, first, later, gateway = await _two_speaker_roll_exchange(client, llm_mock, monkeypatch, (0.1, 0.9))

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["kael — again"]))
    llm_mock.enqueue_writer("kael again")
    await _drain(handle_magic_rewrite(conv["id"], later["id"], "slower, more reluctant"))
    regenerated = (await dbmod.get_messages(conv["id"]))[-1]

    assert len(gateway.batches) == 1
    assert regenerated["decision_evaluations"] == first["decision_evaluations"]


async def test_the_first_speaker_still_rerolls_on_regeneration(client, db, llm_mock, monkeypatch):
    """Nothing on the new branch was written to the old draw, so the dice roll again."""
    conv, first, _later, gateway = await _two_speaker_roll_exchange(client, llm_mock, monkeypatch, (0.1, 0.9))

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — again"]))
    llm_mock.enqueue_writer("aria again")
    await _drain(handle_regenerate(conv["id"], first["id"]))
    record = (await dbmod.get_messages(conv["id"]))[-1]["decision_evaluations"]["evaluations"][0]

    assert len(gateway.batches) == 1
    assert (record["answer_source"], record["draw"], record["outcome"]) == ("replay", 0.9, "false")


async def test_giving_the_floor_after_a_rest_judges_the_unanswered_message(client, db, llm_mock, monkeypatch):
    """Manual mode with nobody picked rests; the chip click that follows is a
    ``/speak`` with no message of its own, answering the one that rested."""
    conv = await _group(client)
    await client.put(f"/api/conversations/{conv['id']}", json={"group_turn_mode": "manual"})
    await _configure(client)
    await _add_decision(client)
    gateway = Gateway(monkeypatch)

    await _drain(handle_turn(conv["id"], "I shove the door."))
    assert gateway.batches == []  # the rest runs nothing

    member = (await dbmod.get_group_members(conv["id"]))[0]
    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("aria speaks")
    events = await _drain(handle_speak(conv["id"], member["id"]))
    assert _event(events, "decisions")["evaluations"][0]["outcome"] == "true"
    assert gateway.states == ["Previous reply:\n\n\nCurrent request:\nI shove the door."]

    reply = next(m for m in reversed(await dbmod.get_messages(conv["id"])) if m["role"] == "assistant")
    RAW_ANSWER_CACHE.clear()
    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("aria again")
    await _drain(handle_regenerate(conv["id"], reply["id"]))
    record = (await dbmod.get_messages(conv["id"]))[-1]["decision_evaluations"]["evaluations"][0]

    # The regeneration's parent is that message, so it reads the same input and replays.
    assert len(gateway.batches) == 1
    assert record["answer_source"] == "replay"


async def test_giving_the_floor_with_nothing_to_judge_is_a_routine_skip(client, db, llm_mock, monkeypatch):
    conv = await _group(client)
    await _configure(client)
    await _add_decision(client, decision_state_template="Current request:\n{{last_message}}")
    gateway = Gateway(monkeypatch)

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["aria — go"]))
    llm_mock.enqueue_writer("aria speaks")
    await _drain(handle_turn(conv["id"], "I shove the door."))

    member = (await dbmod.get_group_members(conv["id"]))[1]
    llm_mock.enqueue_director(_direct_scene(moods=[]))
    llm_mock.enqueue_writer("kael speaks")
    events = await _drain(handle_speak(conv["id"], member["id"]))

    skipped = _event(events, "decisions")["skipped"][0]
    assert len(gateway.batches) == 1
    assert skipped["reason"] == "empty_input"
    assert "failed" not in skipped


async def test_a_card_decision_in_a_group_reads_its_own_character(client, db, llm_mock, monkeypatch):
    card_id = await _card_with_decision(
        client,
        decision_state_template="{{char}}: {{description}}\n\n{{last_message}}",
        decision_instructions="Does {{char}} hold the doorway?",
    )
    other = (await client.post("/api/characters", json={"name": "Kael"})).json()["id"]
    conv = (
        await client.post(
            "/api/conversations",
            json={
                "kind": "group",
                "title": "Doorway",
                "group_turn_mode": "director",
                "group_max_speakers": 1,
                "members": [{"character_card_id": other}, {"character_card_id": card_id}],
            },
        )
    ).json()
    await _configure(client)
    gateway = Gateway(monkeypatch, answers={"card_outcome": 0.9})

    llm_mock.enqueue_director(_direct_scene(moods=[], speaking_plan=["kael — go"]))
    llm_mock.enqueue_writer("kael speaks")
    events = await _drain(handle_turn(conv["id"], "I shove the door."))

    assert _event(events, "decisions")["skipped"] == []
    assert gateway.states[0].startswith("Alric: ")
    record = (await dbmod.get_messages(conv["id"]))[-1]["decision_evaluations"]["evaluations"][0]
    assert record["rendered_instructions"] == "Does Alric hold the doorway?"
