"""High-impact contracts for the Bench 2 harness."""

import json

from scripts.bench.driven.corpus import contexts
from scripts.bench.driven.run import SNAPSHOT, build_plan
from scripts.bench.driven.score import wire


def test_every_context_runs_both_arms_and_the_first_arm_alternates():
    plan = build_plan(5, 2, pilot=True)
    turns = list(plan["turns"].values())
    assert len(turns) == 20
    for repeat in (1, 2):
        firsts = [turns[i]["director"] for i in range(0, 20, 2) if turns[i]["repeat"] == repeat]
        assert firsts == [(index + repeat - 1) % 2 == 0 for index in range(5)]
    for index in range(0, 20, 2):
        pair = turns[index : index + 2]
        assert pair[0]["context"] == pair[1]["context"] and {turn["director"] for turn in pair} == {True, False}


def test_contexts_reach_the_lorebook_and_state_stages():
    for context in contexts():
        # Agentic selection runs only with a non-constant entry; the state step needs a starting inventory to retire from.
        assert any(not entry["constant"] for entry in context["card"]["character_book"]["entries"])
        assert context["inventory"]
        assert context["history"][-1]["role"] == "assistant" and context["user_turn"]


def test_wire_rejects_thinking_on_a_seed_and_the_wrong_upstream(tmp_path):
    transport = SNAPSHOT["transports"]["deepseek"]
    config = transport["model_config"]
    body = {
        **{key: config[key] for key in ("temperature", "top_k", "top_p", "min_p", "repetition_penalty", "max_tokens")},
        **json.loads(config["extra_body"]),
        "model": config["model_name"],
        "reasoning": {"effort": "none", "enabled": False},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    frame = 'data: {"provider": "%s", "choices": [{"delta": {"content": "Hi"}}]}\n'

    def call(name, request, provider):
        folder = tmp_path / name
        folder.mkdir()
        metadata = {"binding": {"key": name}, "target": "/v1/chat/completions", "status": 200, "downstream_complete": True}
        (folder / "metadata.json").write_text(json.dumps(metadata))
        (folder / "request.bin").write_text(json.dumps(request))
        (folder / "response.bin").write_text(frame % provider)

    call("clean", body, transport["provider"])
    call("thinking", {**body, "reasoning": {"enabled": True}}, transport["provider"])
    call("seeded", {**body, "seed": 7}, transport["provider"])
    call("rerouted", body, "Another Upstream")
    errors = {key: set(row["wire_errors"]) for key, row in wire(tmp_path, transport).items()}
    assert errors["clean"] == set()
    assert errors["thinking"] == {"wire.thinking"}
    assert errors["seeded"] == {"wire.seed"}
    assert errors["rerouted"] == {"wire.provider:['Another Upstream']"}
