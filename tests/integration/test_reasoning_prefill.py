"""Turn-level integration tests for the per-pass reasoning prefill.

The prefill is a text-mode prompt tail (see tests/unit/test_text_completion.py for
the transport bytes); what matters here is that the setting round-trips and that
each pass receives *its own* resolved string on the wire — including the editor's
sub-steps (feedback and the post-turn direction note, which ride the editor
toggle and channel). The default-on ``verify_kv_prefix_invariants`` teardown
proves the tail injection did not disturb the shared prefix.
"""

from __future__ import annotations

import backend.database as dbmod
from backend.pipeline import handle_turn

_PREFILLS = {
    "director": "I will direct the scene for {{char}}. First, ",
    "writer": "I will reason in character as {{char}}. First, ",
    "editor": "I will edit {{char}}'s draft. First, ",
}

# 60 words: always trips the 5-word length guard, so the editor pass fires.
_LONG_DRAFT = " ".join(["She nods."] * 30)


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def _setup(client) -> None:
    await client.put(
        "/api/settings",
        json={
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True, "editor_apply_patch": True},
            "length_guard_enabled": True,
            "length_guard_max_words": 5,
            "feedback_enabled": True,
            "direction_notes_record": True,
            "reasoning_enabled_passes": {"director": True, "writer": True, "editor": True},
            "reasoning_prefill_passes": _PREFILLS,
        },
    )
    await client.put("/api/interactive-fragments/suggested_actions", json={"enabled": True})
    await dbmod.create_interactive_fragment(
        {
            "id": "trajectory",
            "label": "Trajectory",
            "description": "Record the direction of travel.",
            "field_type": "direction_note",
            "injection_label": "Direction of travel",
            "enabled": True,
            "direction_note_timing": "post_turn",
        }
    )


def _call(name: str, **args: object) -> list[dict]:
    return [{"type": "function", "function": {"name": name, "arguments": dict(args)}}]


async def test_reasoning_prefill_setting_round_trips(client, db):
    resp = await client.put("/api/settings", json={"reasoning_prefill_passes": _PREFILLS})
    assert resp.status_code == 200
    got = (await client.get("/api/settings")).json()
    # Stored and returned as a decoded dict, macros unresolved (resolution is per-turn).
    assert got["reasoning_prefill_passes"] == _PREFILLS


async def test_each_pass_gets_its_own_resolved_prefill(client, db, llm_mock):
    cid = "conv-reasoning-prefill"
    await dbmod.create_conversation(cid, "rp", "Aria", "a scenario")
    await _setup(client)

    llm_mock.enqueue_director(_call("direct_scene", moods=["vivid"]))
    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)  # no tool call → the edit loop stops after iteration 0
    llm_mock.enqueue_feedback(_call("give_feedback", suggested_actions="Ask her name."))
    llm_mock.enqueue_direction_note(_call("record_direction_note", trajectory="She warms to him."))

    await _drain(handle_turn(cid, "hello"))

    seen: dict[str, set[str]] = {}
    for c in llm_mock.captured:
        seen.setdefault(c["pass"], set()).add(c["params"].get("reasoning_prefill", ""))

    # {{char}} resolved to the conversation's character name; no sharing between passes.
    assert seen["director"] == {"I will direct the scene for Aria. First, "}
    assert seen["writer"] == {"I will reason in character as Aria. First, "}
    # The editor's string also reaches its sub-steps.
    editor_text = "I will edit Aria's draft. First, "
    for name in ("editor", "feedback", "direction_note"):
        assert seen[name] == {editor_text}, f"{name} got {seen.get(name)}"


async def test_prefill_absent_when_pass_reasoning_off(client, db, llm_mock):
    cid = "conv-reasoning-prefill-off"
    await dbmod.create_conversation(cid, "rp", "Aria", "a scenario")
    await _setup(client)
    # Writer reasoning off: reasoning_cfg drops the prefill structurally, so a
    # reasoning-off call can never carry one.
    await client.put(
        "/api/settings",
        json={"reasoning_enabled_passes": {"director": True, "writer": False, "editor": True}},
    )

    llm_mock.enqueue_director(_call("direct_scene", moods=["vivid"]))
    llm_mock.enqueue_writer(_LONG_DRAFT)
    llm_mock.enqueue_editor(None)
    llm_mock.enqueue_feedback(_call("give_feedback", suggested_actions="Ask her name."))
    llm_mock.enqueue_direction_note(_call("record_direction_note", trajectory="She warms to him."))

    await _drain(handle_turn(cid, "hello"))

    for c in llm_mock.captured:
        if c["pass"] == "writer":
            assert "reasoning_prefill" not in c["params"]
        elif c["pass"] == "director":
            assert c["params"]["reasoning_prefill"] == "I will direct the scene for Aria. First, "
