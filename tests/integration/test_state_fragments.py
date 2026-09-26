"""Turn-level integration tests for persistent state fragments.

A state fragment remembers changing conversation state as one value or as
multiple entries. These tests drive real turns through the pipeline with the
LLM mocked, and assert on what the history stores and what each pass receives:
the update transports (direct_scene before the Writer, the ``update_state`` step
before the Writer or after the reply), ``keep`` semantics, validation, branch
behaviour, carried corrections, group exchanges, partial saves, Checkpoint and
Compress History, the State tab routes, and the tools-blob invariants.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import pytest

import backend.database as dbmod
from backend.core import MAX_ACTIVE_ENTRIES
from backend.pipeline import handle_regenerate, handle_turn

_THREAD = "The stolen key is still missing."


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


async def _fragment(client, fid: str, *, mode: str, update: str, inject: str = "both", **extra) -> None:
    payload = {
        "id": fid,
        "label": extra.pop("label", fid.replace("_", " ").title()),
        "description": extra.pop("description", f"Track the {fid}."),
        "field_type": "state",
        "state_mode": mode,
        "state_update": update,
        "state_inject": inject,
        "injection_label": extra.pop("injection_label", fid.replace("_", " ").title()),
        **extra,
    }
    resp = await client.post("/api/interactive-fragments", json=payload)
    assert resp.status_code == 200, resp.text


async def test_reserved_fragment_ids_are_refused(client, db):
    for fid in ("retire", "moods"):
        resp = await client.post(
            "/api/interactive-fragments",
            json={"id": fid, "label": "X", "description": "d", "field_type": "state", "injection_label": "X"},
        )
        assert resp.status_code == 400, fid
        assert "reserved" in resp.json()["detail"]


async def _settings(client, **overrides) -> None:
    body = {"enable_agent": True, "enabled_tools": {"direct_scene": True}, **overrides}
    resp = await client.put("/api/settings", json=body)
    assert resp.status_code == 200, resp.text


def _state_call(**fields) -> list[dict]:
    return [{"type": "function", "function": {"name": "update_state", "arguments": dict(fields)}}]


def _direct(**args) -> list[dict]:
    return [{"type": "function", "function": {"name": "direct_scene", "arguments": {"moods": [], **args}}}]


async def _conversation(cid: str) -> str:
    await dbmod.create_conversation(cid, "state", "Bot", "a scenario")
    return cid


async def _active_state(cid: str) -> dict[str, list[str]]:
    path = await dbmod.get_messages(cid)
    view = await dbmod.fold_path_state(cid, [m["id"] for m in path])
    return {fid: [entry.text for entry in view.active(fid)] for fid in view.entries}


async def _last_assistant(cid: str):
    return [m for m in await dbmod.get_messages(cid) if m["role"] == "assistant"][-1]


def _injection(events: list[dict]) -> str:
    blocks = [e["data"]["injection_block"] for e in events if e.get("event") == "director_done"]
    return blocks[-1] if blocks else ""


def _state_payloads(events: list[dict]) -> list[dict]:
    return [e["data"] for e in events if e.get("event") == "state"]


def _requests(llm_mock, pass_name: str) -> list[str]:
    out = []
    for call in llm_mock.captured:
        if call["pass"] == pass_name:
            content = call["messages"][-1]["content"]
            out.append(content if isinstance(content, str) else json.dumps(content))
    return out


# ── Transports ───────────────────────────────────────────────────────────────


async def test_after_reply_entries_update_replays_the_reply_and_commits_with_it(client, db, llm_mock):
    cid = await _conversation("conv-state-after")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)

    llm_mock.enqueue_writer("She pockets the key.")
    llm_mock.enqueue_state(_state_call(threads=[_THREAD]))

    events = await _drain(handle_turn(cid, "hello"))

    assert {"event": "step_start", "data": {"step": "state"}} in events
    assert [c["text"] for c in _state_payloads(events)[-1]["changes"]] == [_THREAD]
    order = [p for p, _ in llm_mock.calls]
    assert order.index("writer") < order.index("state")
    # The call extends the Writer's exchange rather than forking off the prefix.
    state_call = next(c for c in llm_mock.captured if c["pass"] == "state")
    assert state_call["messages"][-2] == {"role": "assistant", "content": "She pockets the key."}

    rows = await dbmod.get_state_events_for_message((await _last_assistant(cid))["id"])
    assert [(r["fragment_id"], r["op"], r["text"], r["source"], r["mode"]) for r in rows] == [
        ("threads", "add", _THREAD, "agent", "entries")
    ]
    # An after-reply change reaches the Writer on the next reply, not this one.
    assert "Current State" not in _injection(events)
    llm_mock.enqueue_writer("Later.")
    llm_mock.enqueue_state(_state_call())
    later = await _drain(handle_turn(cid, "next"))
    assert f"Threads:\n- {_THREAD}" in _injection(later)


async def test_before_writer_entries_update_reaches_the_same_replys_writer(client, db, llm_mock):
    cid = await _conversation("conv-state-before")
    await _fragment(client, "threads", mode="entries", update="before_writer", inject="writer")
    await _settings(client)

    llm_mock.enqueue_director(_direct())
    llm_mock.enqueue_state(_state_call(threads=[_THREAD]))
    llm_mock.enqueue_writer("He turns away.")

    events = await _drain(handle_turn(cid, "hello"))

    order = [p for p, _ in llm_mock.calls]
    assert order.index("director") < order.index("state") < order.index("writer")
    assert f"Threads:\n- {_THREAD}" in _injection(events)
    # The step sees the user's message and the scene direction it reflects on.
    request = _requests(llm_mock, "state")[0]
    assert '"""hello"""' in request
    assert await _active_state(cid) == {"threads": [_THREAD]}


async def test_before_writer_entries_update_needs_scene_direction(client, db, llm_mock):
    cid = await _conversation("conv-state-before-skip")
    await _fragment(client, "threads", mode="entries", update="before_writer")
    await _settings(client, enabled_tools={"direct_scene": False})

    llm_mock.enqueue_writer("A reply.")
    llm_mock.enqueue_state(_state_call(threads=[_THREAD]))  # must stay unconsumed

    await _drain(handle_turn(cid, "hello"))

    assert "state" not in [p for p, _ in llm_mock.calls]
    assert await _active_state(cid) == {}


async def test_before_writer_value_rides_direct_scene_with_old_to_new_for_the_writer(client, db, llm_mock):
    cid = await _conversation("conv-state-value")
    await _fragment(client, "trust", mode="value", update="before_writer", inject="both")
    await _settings(client)

    llm_mock.enqueue_director(_direct(trust="wary"))
    llm_mock.enqueue_writer("One.")
    first = await _drain(handle_turn(cid, "one"))
    assert "Trust: wary" in _injection(first)
    first_id = (await dbmod.get_state_events_for_message((await _last_assistant(cid))["id"]))[0]["entry_id"]

    llm_mock.enqueue_director(_direct(trust="warming"))
    llm_mock.enqueue_writer("Two.")
    second = await _drain(handle_turn(cid, "two"))

    # The Writer sees the transition on the turn it changes; the Director saw the
    # prior value in its own update lines, not a second time in an injected block.
    assert "Trust: wary -> warming" in _injection(second)
    director_request = _requests(llm_mock, "director")[-1]
    assert "Saved state fields - leave a field empty to keep it" in director_request
    assert "wary" in director_request
    assert "Current State" not in director_request
    # The one value keeps its entry id across sets.
    rows = await dbmod.get_state_events_for_message((await _last_assistant(cid))["id"])
    assert [(r["op"], r["entry_id"], r["text"]) for r in rows] == [("revise", first_id, "warming")]
    # No state step: the value rode the Director's existing call.
    assert "state" not in [p for p, _ in llm_mock.calls]

    # Omitted -- or empty -- keeps the value, and the Writer still sees it.
    llm_mock.enqueue_director(_direct(trust=""))
    llm_mock.enqueue_writer("Three.")
    third = await _drain(handle_turn(cid, "three"))
    assert "Trust: warming" in _injection(third)
    assert await _active_state(cid) == {"trust": ["warming"]}


async def test_migrated_progressive_settings_add_no_model_call(client, db, llm_mock):
    """One value, before the Writer, into both passes: exactly the calls a
    progressive fragment cost -- the Director and the Writer."""
    cid = await _conversation("conv-state-progressive-parity")
    await _fragment(client, "trust", mode="value", update="before_writer", inject="both")
    await _settings(client)

    llm_mock.enqueue_director(_direct(trust="wary"))
    llm_mock.enqueue_writer("One.")
    await _drain(handle_turn(cid, "one"))

    assert [p for p, _ in llm_mock.calls] == ["director", "writer"]


# ── Switches and gates ───────────────────────────────────────────────────────


async def test_manual_only_stops_automatic_updates_and_keeps_injection(client, db, llm_mock):
    cid = await _conversation("conv-state-switch")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _fragment(client, "trust", mode="value", update="before_writer")
    await client.put("/api/interactive-fragments/threads", json={"enabled": True})
    await _settings(client)
    llm_mock.enqueue_director(_direct(trust="wary"))
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(threads=[_THREAD]))
    await _drain(handle_turn(cid, "one"))

    for fid in ("threads", "trust"):
        resp = await client.put(f"/api/interactive-fragments/{fid}", json={"state_update": "manual"})
        assert resp.status_code == 200, resp.text
    llm_mock.enqueue_director(_direct(trust="ignored"))
    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state(_state_call(threads=["ignored"]))  # must stay unconsumed
    events = await _drain(handle_turn(cid, "two"))

    assert [p for p, _ in llm_mock.calls].count("state") == 1
    # The one-value field left the schema entirely, and injection is independent.
    director_tools = [c["tools"] for c in llm_mock.captured if c["pass"] == "director"][-1]
    direct_scene = next(t for t in director_tools if t["function"]["name"] == "direct_scene")
    assert "trust" not in direct_scene["function"]["parameters"]["properties"]
    assert "Trust: wary" in _injection(events)
    assert await _active_state(cid) == {"threads": [_THREAD], "trust": ["wary"]}


async def test_agent_toggle_and_manual_only_make_no_update_call(client, db, llm_mock):
    cid = await _conversation("conv-state-gates")
    await _fragment(client, "notes_by_hand", mode="entries", update="manual", inject="writer")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client, enable_agent=False)

    llm_mock.enqueue_writer("A reply.")
    llm_mock.enqueue_state(_state_call(threads=[_THREAD]))  # must stay unconsumed
    await _drain(handle_turn(cid, "hello"))
    assert "state" not in [p for p, _ in llm_mock.calls]

    # A manual-only fragment never makes a model call, even with the Agent on.
    await client.put("/api/interactive-fragments/threads", json={"enabled": False})
    await _settings(client, enable_agent=True)
    llm_mock.enqueue_writer("Another.")
    await _drain(handle_turn(cid, "again"))
    assert "state" not in [p for p, _ in llm_mock.calls]


async def test_empty_draft_persists_no_change(client, db, llm_mock):
    cid = await _conversation("conv-state-empty")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)

    llm_mock.enqueue_writer("")
    llm_mock.enqueue_state(_state_call(threads=[_THREAD]))  # must stay unconsumed
    await _drain(handle_turn(cid, "hello"))

    assert "state" not in [p for p, _ in llm_mock.calls]
    assert await _active_state(cid) == {}


async def test_disabled_fragment_is_neither_updated_nor_injected(client, db, llm_mock):
    cid = await _conversation("conv-state-disabled")
    await _fragment(client, "threads", mode="entries", update="after_reply", inject="writer")
    await _settings(client)
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(threads=[_THREAD]))
    await _drain(handle_turn(cid, "one"))

    await client.put("/api/interactive-fragments/threads", json={"enabled": False})
    llm_mock.enqueue_writer("Two.")
    events = await _drain(handle_turn(cid, "two"))

    assert [p for p, _ in llm_mock.calls].count("state") == 1
    assert "Threads" not in _injection(events)
    panel = (await client.get(f"/api/conversations/{cid}/state")).json()
    threads = next(f for f in panel["fragments"] if f["fragment_id"] == "threads")
    # Disabling stops updates but keeps the saved state, shown read-only.
    assert threads["read_only"] and [e["text"] for e in threads["entries"]] == [_THREAD]


async def test_injection_targets_are_per_fragment(client, db, llm_mock):
    cid = await _conversation("conv-state-inject")
    for fid, inject in (("to_director", "director"), ("to_writer", "writer"), ("to_both", "both"), ("to_none", "off")):
        await _fragment(client, fid, mode="entries", update="manual", inject=inject)
    await _settings(client)
    await dbmod.add_message(cid, "assistant", "Greeting.", 0, advance_leaf=True)
    for fid in ("to_director", "to_writer", "to_both", "to_none"):
        resp = await client.post(f"/api/conversations/{cid}/state", json={"fragment_id": fid, "op": "add", "text": fid})
        assert resp.status_code == 200, resp.text

    llm_mock.enqueue_director(_direct())
    llm_mock.enqueue_writer("A reply.")
    events = await _drain(handle_turn(cid, "hello"))

    writer_block = _injection(events)
    director_request = _requests(llm_mock, "director")[0]
    assert "- to_writer" in writer_block and "- to_both" in writer_block
    assert "- to_director" not in writer_block and "- to_none" not in writer_block
    assert "- to_director" in director_request and "- to_both" in director_request
    assert "- to_writer" not in director_request and "- to_none" not in director_request


# ── Operations, validation, and keep ─────────────────────────────────────────


async def test_retire_by_alias_frees_a_slot_and_revision_is_retire_plus_add(client, db, llm_mock):
    cid = await _conversation("conv-state-retire")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(threads=["Find the key.", "Meet the smuggler."]))
    await _drain(handle_turn(cid, "one"))

    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state(_state_call(retire=["e1"], threads=["Find the key before dawn."]))
    await _drain(handle_turn(cid, "two"))

    # The request listed entries by alias, never by id.
    request = _requests(llm_mock, "state")[-1]
    assert "- [e1] Find the key." in request and "- [e2] Meet the smuggler." in request
    assert "2 of 12 entries" in request
    assert await _active_state(cid) == {"threads": ["Meet the smuggler.", "Find the key before dawn."]}


async def test_keep_semantics_for_skipped_empty_and_malformed_calls(client, db, llm_mock):
    cid = await _conversation("conv-state-keep")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _fragment(client, "mood_value", mode="value", update="after_reply")
    await _settings(client)
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(threads=[_THREAD], mood_value="calm"))
    await _drain(handle_turn(cid, "one"))
    before = await _active_state(cid)

    # Skipped (no tool call), empty values, an object where a list belongs, a
    # list where one value belongs, an unknown alias, and an unknown field: none
    # of it changes the state.
    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state([])
    await _drain(handle_turn(cid, "two"))
    llm_mock.enqueue_writer("Three.")
    llm_mock.enqueue_state(_state_call(threads=[], mood_value="", retire=[]))
    await _drain(handle_turn(cid, "three"))
    llm_mock.enqueue_writer("Four.")
    llm_mock.enqueue_state(_state_call(threads={"text": "not a list"}, retire=["e9"], ghost="boo", mood_value=["x"]))
    events = await _drain(handle_turn(cid, "four"))

    assert await _active_state(cid) == before
    rejected = _state_payloads(events)[-1]["rejected"]
    assert {r["reason"] for r in rejected} == {"malformed", "unknown_entry", "unknown_fragment"}
    # The Inspector reads them back from the reply's log.
    last = await _last_assistant(cid)
    log = (await client.get(f"/api/conversations/{cid}/messages/{last['id']}/director-log")).json()
    assert {r["reason"] for r in log["state"]["rejected"]} == {"malformed", "unknown_entry", "unknown_fragment"}
    assert log["state"]["changes"] == []


async def test_over_limit_add_is_rejected_and_the_fragment_shows_full(client, db, llm_mock):
    cid = await _conversation("conv-state-full")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(threads=[f"thread {i}" for i in range(MAX_ACTIVE_ENTRIES + 2)]))
    events = await _drain(handle_turn(cid, "one"))

    assert len((await _active_state(cid))["threads"]) == MAX_ACTIVE_ENTRIES
    assert [r["reason"] for r in _state_payloads(events)[-1]["rejected"]] == ["full", "full"]
    panel = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert next(f for f in panel["fragments"] if f["fragment_id"] == "threads")["full"] is True

    # The next request tells the updater the list is full.
    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state([])
    await _drain(handle_turn(cid, "two"))
    assert "This list is full" in _requests(llm_mock, "state")[-1]


async def test_per_fragment_option_makes_one_call_per_fragment_with_its_own_aliases(client, db, llm_mock):
    cid = await _conversation("conv-state-perfrag")
    await _fragment(client, "alpha", mode="entries", update="after_reply")
    await _fragment(client, "beta", mode="entries", update="after_reply")
    await _settings(client, director_individual_fragments=True, enabled_tools={"direct_scene": False})
    llm_mock.enqueue_writer("One.")
    # The wire schema is the shared union, so a reply may fill both; each call
    # keeps only its own fragment.
    both = _state_call(alpha=["a-1"], beta=["b-1"])
    llm_mock.enqueue_state(both)
    llm_mock.enqueue_state(both)
    await _drain(handle_turn(cid, "one"))
    assert [p for p, _ in llm_mock.calls].count("state") == 2
    assert await _active_state(cid) == {"alpha": ["a-1"], "beta": ["b-1"]}

    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state([])
    llm_mock.enqueue_state([])
    await _drain(handle_turn(cid, "two"))
    requests = _requests(llm_mock, "state")[-2:]
    # Aliases restart for each call.
    assert "- [e1] a-1" in requests[0] and "b-1" not in requests[0]
    assert "- [e1] b-1" in requests[1] and "a-1" not in requests[1]


async def test_a_judge_outcome_is_context_for_the_updater_not_a_write(client, db, llm_mock):
    from backend.pipeline.passes.judge import JudgeResult

    cid = await _conversation("conv-state-judge")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)
    guidance = "**Decisions**\nOutcome: That action fails hard."

    async def judged(*_args, **_kwargs):
        return JudgeResult(guidance=guidance)

    llm_mock.enqueue_writer("The lock holds.")
    llm_mock.enqueue_state([])
    with patch("backend.pipeline.entrypoints.judge_pass", new=judged):
        await _drain(handle_turn(cid, "I pick the lock."))

    # It reaches the updater inside the replayed Writer message, never restated
    # in the request, where it would read as a second account of the reply.
    state_call = next(c for c in llm_mock.captured if c["pass"] == "state")
    assert "That action fails hard." in json.dumps(state_call["messages"][-3]["content"])
    assert "That action fails hard." not in _requests(llm_mock, "state")[0]
    # Only the updater's own operations write state; the outcome itself writes none.
    assert await _active_state(cid) == {}


# ── Modes ────────────────────────────────────────────────────────────────────


# Editing a fragment's mode is a configuration change: it rebuilds the schema on
# the next call by design, like any fragment edit. State writes never do.
@pytest.mark.kv_divergence_expected
async def test_mode_switch_writes_nothing_and_keeps_entry_ids(client, db, llm_mock):
    cid = await _conversation("conv-state-mode")
    await _fragment(client, "place", mode="value", update="after_reply", inject="writer")
    await _settings(client)
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(place="The docks."))
    await _drain(handle_turn(cid, "one"))
    all_events = await db.execute_fetchall("SELECT COUNT(*) AS n FROM fragment_state_events")
    count = all_events[0]["n"]
    original_id = (await dbmod.get_state_events_for_message((await _last_assistant(cid))["id"]))[0]["entry_id"]

    # To multiple entries: no event, same entry, same id.
    resp = await client.put("/api/interactive-fragments/place", json={"state_mode": "entries"})
    assert resp.status_code == 200 and resp.json()["state_mode"] == "entries"
    assert (await db.execute_fetchall("SELECT COUNT(*) AS n FROM fragment_state_events"))[0]["n"] == count
    panel = (await client.get(f"/api/conversations/{cid}/state")).json()
    place = next(f for f in panel["fragments"] if f["fragment_id"] == "place")
    assert [(e["entry_id"], e["text"]) for e in place["entries"]] == [(original_id, "The docks.")]

    # The schema rebuilds on the next call: the field is now a list of new entries.
    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state(_state_call(place=["The warehouse."]))
    await _drain(handle_turn(cid, "two"))
    tools = [c["tools"] for c in llm_mock.captured if c["pass"] == "state"]
    shapes = [next(t for t in blob if t["function"]["name"] == "update_state") for blob in tools]
    assert shapes[0]["function"]["parameters"]["properties"]["place"]["type"] == "string"
    assert shapes[1]["function"]["parameters"]["properties"]["place"]["type"] == "array"

    # Back to one value: both entries stay, render together everywhere, and the
    # panel says the next update replaces them.
    await client.put("/api/interactive-fragments/place", json={"state_mode": "value"})
    panel = (await client.get(f"/api/conversations/{cid}/state")).json()
    assert next(f for f in panel["fragments"] if f["fragment_id"] == "place")["several_values"] is True
    llm_mock.enqueue_writer("Three.")
    llm_mock.enqueue_state(_state_call(place="The rooftop."))
    events = await _drain(handle_turn(cid, "three"))
    assert "Place:\n- The docks.\n- The warehouse." in _injection(events)
    request = _requests(llm_mock, "state")[-1]
    assert "several entries; a new value replaces them all" in request

    # The next set retires them all and adds one; the originals remain in history.
    rows = await dbmod.get_state_events_for_message((await _last_assistant(cid))["id"])
    assert [r["op"] for r in rows] == ["retire", "retire", "add"]
    assert await _active_state(cid) == {"place": ["The rooftop."]}
    history = (await client.get(f"/api/conversations/{cid}/state/history", params={"fragment_id": "place"})).json()
    assert {h["text"] for h in history if h["op"] == "add"} >= {"The docks.", "The warehouse.", "The rooftop."}


# ── Branches, regeneration, and carried corrections ─────────────────────────


async def test_regeneration_starts_from_the_parent_path_and_branches_keep_their_state(client, db, llm_mock):
    cid = await _conversation("conv-state-branch")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(threads=["first"]))
    await _drain(handle_turn(cid, "one"))
    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state(_state_call(threads=["second"]))
    await _drain(handle_turn(cid, "two"))
    original = await _last_assistant(cid)

    llm_mock.enqueue_writer("Two, again.")
    llm_mock.enqueue_state(_state_call(threads=["other"]))
    await _drain(handle_regenerate(cid, original["id"]))

    # The regeneration saw only the parent path's state, never its sibling's change.
    assert "second" not in _requests(llm_mock, "state")[-1]
    assert await _active_state(cid) == {"threads": ["first", "other"]}
    await client.post(f"/api/conversations/{cid}/messages/{original['id']}/switch-branch")
    assert await _active_state(cid) == {"threads": ["first", "second"]}


async def test_manual_correction_is_carried_to_the_regenerated_reply(client, db, llm_mock):
    cid = await _conversation("conv-state-carry")
    await _fragment(client, "threads", mode="entries", update="after_reply", inject="writer")
    await _settings(client)
    llm_mock.enqueue_writer("One.")
    llm_mock.enqueue_state(_state_call(threads=["kept"]))
    await _drain(handle_turn(cid, "one"))
    llm_mock.enqueue_writer("Two.")
    llm_mock.enqueue_state(_state_call(threads=["from the reply"]))
    await _drain(handle_turn(cid, "two"))
    target = await _last_assistant(cid)
    panel = (await client.get(f"/api/conversations/{cid}/state")).json()
    threads = next(f for f in panel["fragments"] if f["fragment_id"] == "threads")
    entries = {e["text"]: e["entry_id"] for e in threads["entries"]}

    # Three corrections anchored on the reply: an add (always carries), a revise
    # of an entry from an earlier turn (carries), and a retire of the entry the
    # reply itself added (nothing to apply to once the reply is regenerated).
    for body in (
        {"fragment_id": "threads", "op": "add", "text": "user fact"},
        {"fragment_id": "threads", "op": "revise", "entry_id": entries["kept"], "text": "kept, corrected"},
        {"fragment_id": "threads", "op": "retire", "entry_id": entries["from the reply"]},
    ):
        resp = await client.post(f"/api/conversations/{cid}/state", json=body)
        assert resp.status_code == 200, resp.text

    llm_mock.enqueue_writer("Two, again.")
    llm_mock.enqueue_state(_state_call())
    events = await _drain(handle_regenerate(cid, target["id"]))

    # The regenerated reply is written with the correction.
    assert "- kept, corrected" in _injection(events) and "- user fact" in _injection(events)
    assert "from the reply" not in _injection(events)
    dropped = _state_payloads(events)[0]["dropped"]
    assert [(d["op"], d["entry_id"]) for d in dropped] == [("retire", entries["from the reply"])]
    new_reply = await _last_assistant(cid)
    rows = await dbmod.get_state_events_for_message(new_reply["id"])
    # Carried user events come first, in their original order, still the user's.
    assert [(r["op"], r["text"], r["source"]) for r in rows] == [
        ("add", "user fact", "user"),
        ("revise", "kept, corrected", "user"),
    ]
    assert await _active_state(cid) == {"threads": ["kept, corrected", "user fact"]}
    log = (await client.get(f"/api/conversations/{cid}/messages/{new_reply['id']}/director-log")).json()
    assert [d["entry_id"] for d in log["state"]["dropped"]] == [entries["from the reply"]]


async def test_regeneration_folds_state_anchored_on_its_parent_user_message(client, db, llm_mock):
    cid = await _conversation("conv-state-regen-parent")
    await _fragment(client, "place", mode="value", update="after_reply", inject="writer")
    await _settings(client)
    greeting, _ = await dbmod.add_message(cid, "assistant", "Greeting.", 0, advance_leaf=True)
    user, _ = await dbmod.add_message(cid, "user", "hi", 1, parent_id=greeting, advance_leaf=True)
    # The user message is the leaf (its reply failed, say), so the correction anchors there.
    resp = await client.post(f"/api/conversations/{cid}/state", json={"fragment_id": "place", "op": "set", "text": "Docks"})
    assert resp.status_code == 200, resp.text
    reply, _ = await dbmod.add_message(cid, "assistant", "At the docks.", 2, parent_id=user, advance_leaf=True)

    llm_mock.enqueue_writer("Still at the docks.")
    llm_mock.enqueue_state(_state_call(place="Harbor"))
    events = await _drain(handle_regenerate(cid, reply))

    # The regeneration starts from the same state as the reply it replaces, so the
    # model's new value revises the user's entry instead of adding a second one.
    assert "Place: Docks" in _injection(events)
    assert "Current value: Docks" in _requests(llm_mock, "state")[-1]
    assert await _active_state(cid) == {"place": ["Harbor"]}


# ── Group exchanges ──────────────────────────────────────────────────────────


async def _group(client) -> str:
    aria = (await client.post("/api/characters", json={"name": "Aria"})).json()["id"]
    kael = (await client.post("/api/characters", json={"name": "Kael"})).json()["id"]
    conv = (
        await client.post(
            "/api/conversations",
            json={
                "kind": "group",
                "title": "Campfire",
                "members": [{"character_card_id": aria}, {"character_card_id": kael}],
            },
        )
    ).json()
    return conv["id"]


def _plan() -> list[dict]:
    return _direct(speaking_plan=["aria — Notice", "kael — Answer"])


async def test_group_exchange_updates_state_once_and_anchors_each_placement(client, db, llm_mock):
    cid = await _group(client)
    await _fragment(client, "early", mode="entries", update="before_writer", inject="writer")
    await _fragment(client, "late", mode="entries", update="after_reply")
    await _settings(client)

    llm_mock.enqueue_director(_plan())
    llm_mock.enqueue_state(_state_call(early=["before-note"]))
    llm_mock.enqueue_writer("Aria speaks.")
    llm_mock.enqueue_writer("Kael answers.")
    llm_mock.enqueue_state(_state_call(late=["after-note"]))

    await client.post(f"/api/conversations/{cid}/send", json={"content": "What happened?"})

    assert [p for p, _ in llm_mock.calls].count("state") == 2
    replies = [m for m in await dbmod.get_messages(cid) if m["role"] == "assistant"]
    first = [r["text"] for r in await dbmod.get_state_events_for_message(replies[0]["id"])]
    last = [r["text"] for r in await dbmod.get_state_events_for_message(replies[1]["id"])]
    assert first == ["before-note"]
    assert last == ["after-note"]
    # Every speaker wrote with the exchange's before-Writer state.
    writer_calls = [c for c in llm_mock.captured if c["pass"] == "writer"]
    assert all("before-note" in json.dumps(c["messages"]) for c in writer_calls)


# ── Partial saves ────────────────────────────────────────────────────────────


async def test_stopped_reply_keeps_its_before_writer_changes(client, db, llm_mock):
    cid = await _conversation("conv-state-stop")
    await _fragment(client, "trust", mode="value", update="before_writer")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)

    async def stopped_writer(c, *args, **kwargs):
        yield {"type": "content", "delta": "Partial te"}
        c.abort()

    llm_mock.enqueue_director(_direct(trust="wary"))
    with patch("backend.pipeline.passes.writer.writer_pass", new=stopped_writer):
        await _drain(handle_turn(cid, "hello"))

    reply = await _last_assistant(cid)
    assert reply["content"] == "Partial te"
    assert "state" not in [p for p, _ in llm_mock.calls]  # after-reply never runs
    assert await _active_state(cid) == {"trust": ["wary"]}


async def test_fallback_save_keeps_the_announced_before_writer_changes(client, db):
    """A turn cut off before ``_result`` saves its streamed text with the
    before-Writer changes on the live state announced ahead of the Writer."""
    from backend.pipeline.persistence import _consume_pipeline
    from backend.pipeline.state import TurnState

    cid = await _conversation("conv-state-fallback")
    user_id, _ = await dbmod.add_message(cid, "user", "hello", 0, advance_leaf=True)
    change = {
        "fragment_id": "trust",
        "entry_id": "e-trust",
        "op": "add",
        "text": "wary",
        "mode": "value",
        "fragment_label": "Trust",
        "source": "agent",
    }

    async def cut_off():
        state = TurnState(state_events=[change])
        yield {"event": "_turn_state", "data": state}
        state.resp_text += "Partial"
        yield {"event": "token", "data": "Partial"}
        raise RuntimeError("connection lost")

    settings = await dbmod.get_settings()
    events = []
    try:
        async for event in _consume_pipeline(cut_off(), cid, settings, user_id, 1):
            events.append(event)
    except RuntimeError:
        pass

    assert not any(e["event"].startswith("_") for e in events), "internal events must not reach the browser"
    reply = await _last_assistant(cid)
    assert reply["content"] == "Partial"
    assert [r["text"] for r in await dbmod.get_state_events_for_message(reply["id"])] == ["wary"]


# ── Checkpoint and Compress History ─────────────────────────────────────────


async def test_compress_carries_state_and_regenerating_the_first_kept_reply_does_not_double_apply(client, db, llm_mock):
    cid = await _conversation("conv-state-compress")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _settings(client)
    for i, thread in enumerate(("one", "two", "three")):
        llm_mock.enqueue_writer(f"Reply {i}.")
        llm_mock.enqueue_state(_state_call(threads=[thread]))
        await _drain(handle_turn(cid, f"msg {i}"))

    resp = await client.post(f"/api/conversations/{cid}/compress", json={"summary": "So far.", "keep_count": 2})
    new_cid = resp.json()["new_conversation_id"]
    assert await _active_state(new_cid) == {"threads": ["one", "two", "three"]}
    messages = await dbmod.get_messages(new_cid)
    summary_rows = await dbmod.get_state_events_for_message(messages[0]["id"])
    assert [(r["text"], r["source"]) for r in summary_rows] == [("one", "carried"), ("two", "carried")]

    # Regenerating the first kept reply starts from the snapshot, without its own change.
    first_kept_reply = messages[2]
    llm_mock.enqueue_writer("Reply 2, again.")
    llm_mock.enqueue_state(_state_call(threads=["three, again"]))
    await _drain(handle_regenerate(new_cid, first_kept_reply["id"]))
    request = _requests(llm_mock, "state")[-1]
    assert "- [e1] one" in request and "- [e2] two" in request and "[e3]" not in request
    assert await _active_state(new_cid) == {"threads": ["one", "two", "three, again"]}


# ── State tab routes ─────────────────────────────────────────────────────────


async def test_manual_operations_anchor_to_the_active_leaf_and_validate(client, db, llm_mock):
    cid = await _conversation("conv-state-manual")
    await _fragment(client, "place", mode="value", update="manual")
    await _fragment(client, "threads", mode="entries", update="manual")
    leaf, _ = await dbmod.add_message(cid, "assistant", "Greeting.", 0, advance_leaf=True)

    ok = await client.post(f"/api/conversations/{cid}/state", json={"fragment_id": "place", "op": "set", "text": "Docks"})
    assert ok.status_code == 200
    rows = await dbmod.get_state_events_for_message(leaf)
    assert [(r["op"], r["source"]) for r in rows] == [("add", "user")]

    cases = [
        ({"fragment_id": "place", "op": "add", "text": "x"}, 422),  # wrong mode
        ({"fragment_id": "threads", "op": "add", "text": "x" * 801}, 422),  # too long
        ({"fragment_id": "threads", "op": "retire", "entry_id": "nope"}, 422),  # unknown entry
        ({"fragment_id": "ghost", "op": "add", "text": "x"}, 409),  # no such fragment
        ({"fragment_id": "threads", "op": "add", "text": "   "}, 400),  # empty
    ]
    for body, status in cases:
        resp = await client.post(f"/api/conversations/{cid}/state", json=body)
        assert resp.status_code == status, (body, resp.text)

    cleared = await client.post(f"/api/conversations/{cid}/state", json={"fragment_id": "place", "op": "clear"})
    assert cleared.status_code == 200
    assert await _active_state(cid) == {}


async def test_manual_write_during_generation_is_rejected_as_busy(streaming_client, llm_mock):
    cid = (await streaming_client.post("/api/conversations", json={"title": "busy"})).json()["id"]
    await _fragment(streaming_client, "threads", mode="entries", update="manual")
    leaf, _ = await dbmod.add_message(cid, "user", "hi", 0, advance_leaf=True)
    gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("reply")

    async with streaming_client.stream("POST", f"/api/conversations/{cid}/continue", json={}) as resp:
        assert resp.status_code == 200
        await gate.reached.wait()
        busy = await streaming_client.post(
            f"/api/conversations/{cid}/state", json={"fragment_id": "threads", "op": "add", "text": "x"}
        )
        gate.release.set()
        async for _ in resp.aiter_lines():
            pass

    assert busy.status_code == 409
    assert await dbmod.get_state_events_for_message(leaf) == []


async def test_mid_turn_setting_edit_does_not_change_the_captured_contract(client, db, llm_mock):
    cid = await _conversation("conv-state-contract")
    await _fragment(client, "place", mode="entries", update="after_reply")
    await _settings(client)
    gate = llm_mock.gate("writer")
    llm_mock.enqueue_writer("A reply.")
    # Shaped for the contract the turn started with: a list of new entries.
    llm_mock.enqueue_state(_state_call(place=["The docks."]))

    turn = asyncio.create_task(_drain(handle_turn(cid, "hello")))
    await gate.reached.wait()
    await client.put("/api/interactive-fragments/place", json={"state_mode": "value"})
    gate.release.set()
    events = await turn

    assert await _active_state(cid) == {"place": ["The docks."]}
    assert _state_payloads(events)[-1]["rejected"] == []


async def test_deleted_fragment_state_is_read_only_and_deletable(client, db, llm_mock):
    cid = await _conversation("conv-state-deleted")
    await _fragment(client, "threads", mode="entries", update="manual", label="Old threads")
    await dbmod.add_message(cid, "assistant", "Greeting.", 0, advance_leaf=True)
    await client.post(f"/api/conversations/{cid}/state", json={"fragment_id": "threads", "op": "add", "text": "x"})
    await client.delete("/api/interactive-fragments/threads")

    panel = (await client.get(f"/api/conversations/{cid}/state")).json()
    orphan = next(f for f in panel["fragments"] if f["fragment_id"] == "threads")
    assert orphan["configured"] is False and orphan["read_only"] is True and orphan["label"] == "Old threads"
    edit = await client.post(f"/api/conversations/{cid}/state", json={"fragment_id": "threads", "op": "add", "text": "y"})
    assert edit.status_code == 409

    assert (await client.delete(f"/api/conversations/{cid}/state/threads")).json() == {"deleted": 1}
    assert (await client.get(f"/api/conversations/{cid}/state")).json()["has_state"] is False


# ── Cache invariants ─────────────────────────────────────────────────────────


async def test_tools_blob_is_byte_identical_while_state_changes(client, db, llm_mock):
    cid = await _conversation("conv-state-blob")
    await _fragment(client, "threads", mode="entries", update="after_reply")
    await _fragment(client, "trust", mode="value", update="before_writer")
    await _settings(client)
    for i in range(3):
        llm_mock.enqueue_director(_direct(trust=f"level {i}"))
        llm_mock.enqueue_writer(f"Reply {i}.")
        llm_mock.enqueue_state(_state_call(threads=[f"thread {i}"], retire=["e1"] if i else []))
        await _drain(handle_turn(cid, f"msg {i}"))
    target = await _last_assistant(cid)
    llm_mock.enqueue_director(_direct(trust="other"))
    llm_mock.enqueue_writer("Redo.")
    llm_mock.enqueue_state(_state_call(threads=["redo"]))
    await _drain(handle_regenerate(cid, target["id"]))

    blobs = {json.dumps(c["tools"], separators=(",", ":")) for c in llm_mock.captured if c["tools"]}
    assert len(blobs) == 1, "a state write or branch switch changed the shared tools blob"
    blob = next(iter(blobs))
    for text in ("thread 0", "level 1", "redo"):
        assert text not in blob, f"volatile state {text!r} leaked into the schema"
