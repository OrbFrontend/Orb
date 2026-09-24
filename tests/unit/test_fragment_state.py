"""Unit tests for the state-fragment core: fold, operation contract, parsing, rendering."""

from __future__ import annotations

import itertools

from backend.core import (
    MAX_ACTIVE_ENTRIES,
    MAX_STATE_TEXT_CHARS,
    StateFragment,
    StateOp,
    StateView,
    carry_events,
    fold_events,
    plan_state_ops,
    state_fragment_of,
    upgrade_legacy_fragment,
    value_text,
)
from backend.pipeline.passes.state import (
    StateContract,
    build_state_request,
    entry_aliases,
    parse_state_call,
)
from backend.prompting import render_state_block
from backend.prompting.tool_schemas import build_state_tool

VALUE = StateFragment("place", "Place", "Place", "Where the scene is.", mode="value")
ENTRIES = StateFragment("threads", "Threads", "Open threads", "Unresolved threads.", mode="entries")
FRAGMENTS = {VALUE.id: VALUE, ENTRIES.id: ENTRIES}


def _ids():
    counter = itertools.count(1)
    return lambda: f"id{next(counter)}"


def _add(fid: str, entry_id: str, text: str, **extra) -> dict:
    return {"fragment_id": fid, "entry_id": entry_id, "op": "add", "text": text, **extra}


# ── The fold ─────────────────────────────────────────────────────────────────


def test_fold_applies_explicit_events_in_order_and_ignores_missing_entries():
    view = fold_events(
        [
            _add("threads", "a", "one", fragment_label="Threads"),
            _add("threads", "b", "two"),
            {"fragment_id": "threads", "entry_id": "a", "op": "revise", "text": "one, revised"},
            {"fragment_id": "threads", "entry_id": "zz", "op": "revise", "text": "ghost"},
            {"fragment_id": "threads", "entry_id": "b", "op": "retire"},
            {"fragment_id": "threads", "entry_id": "b", "op": "retire"},
        ]
    )
    assert [(e.entry_id, e.text) for e in view.active("threads")] == [("a", "one, revised")]
    assert view.labels == {"threads": "Threads"}


def test_fold_is_mode_agnostic_and_does_not_mutate_its_base():
    base = fold_events([_add("place", "a", "docks"), _add("place", "b", "warehouse")])
    extended = fold_events([{"fragment_id": "place", "entry_id": "a", "op": "retire"}], base)
    # The same events fold the same way whatever the fragment's mode is now.
    assert [e.text for e in base.active("place")] == ["docks", "warehouse"]
    assert [e.text for e in extended.active("place")] == ["warehouse"]
    assert "place" not in fold_events([{"fragment_id": "place", "entry_id": "b", "op": "retire"}], extended).entries


def test_value_text_renders_several_entries_as_a_list():
    assert value_text([]) == ""
    view = fold_events([_add("place", "a", "docks"), _add("place", "b", "roof")])
    assert value_text(view.active("place")[:1]) == "docks"
    assert value_text(view.active("place")) == "\n- docks\n- roof"


# ── The operation contract ───────────────────────────────────────────────────


def test_one_value_set_creates_then_retains_the_entry_id():
    view = StateView()
    new_id = _ids()
    events, _ = plan_state_ops([StateOp("set", "place", text="docks")], FRAGMENTS, view, source="agent", new_id=new_id)
    assert [(e["op"], e["entry_id"]) for e in events] == [("add", "id1")]
    events, _ = plan_state_ops([StateOp("set", "place", text="roof")], FRAGMENTS, view, source="agent", new_id=new_id)
    assert [(e["op"], e["entry_id"], e["text"]) for e in events] == [("revise", "id1", "roof")]
    # Setting the same text again is not a change.
    events, _ = plan_state_ops([StateOp("set", "place", text="roof")], FRAGMENTS, view, source="agent", new_id=new_id)
    assert events == []


def test_one_value_set_over_several_entries_retires_them_all_and_adds_one():
    view = fold_events([_add("place", "a", "docks"), _add("place", "b", "roof")])
    events, _ = plan_state_ops([StateOp("set", "place", text="alley")], FRAGMENTS, view, source="agent", new_id=_ids())
    assert [(e["op"], e["entry_id"]) for e in events] == [("retire", "a"), ("retire", "b"), ("add", "id1")]
    # A retire keeps the text it retired, so history and the Inspector can say what went.
    assert [e["text"] for e in events] == ["docks", "roof", "alley"]
    assert [e.text for e in view.active("place")] == ["alley"]


def test_empty_text_keeps_and_clear_is_the_users_alone():
    view = fold_events([_add("place", "a", "docks")])
    for op in (StateOp("set", "place", text="   "), StateOp("set", "place")):
        events, rejections = plan_state_ops([op], FRAGMENTS, view, source="agent")
        assert events == [] and rejections == []
    _, rejections = plan_state_ops([StateOp("clear", "place")], FRAGMENTS, view, source="agent")
    assert [r.reason for r in rejections] == ["wrong_mode"]
    events, _ = plan_state_ops([StateOp("clear", "place")], FRAGMENTS, view, source="user")
    assert [(e["op"], e["entry_id"]) for e in events] == [("retire", "a")]
    assert view.active("place") == []


def test_entries_ops_validate_before_writing():
    view = fold_events([_add("threads", "a", "one")])
    ops = [
        StateOp("add", "ghost", text="x"),
        StateOp("set", "threads", text="x"),
        StateOp("add", "threads", text="x" * (MAX_STATE_TEXT_CHARS + 1)),
        StateOp("retire", "threads", entry_id="nope", alias="e9"),
        StateOp("add", "threads", text="  ONE "),
        StateOp("revise", "threads", entry_id="a", text="edited"),
    ]
    events, rejections = plan_state_ops(ops, FRAGMENTS, view, source="agent")
    assert events == []
    assert [r.reason for r in rejections] == [
        "unknown_fragment",
        "wrong_mode",
        "too_long",
        "unknown_entry",
        "duplicate",
        "wrong_mode",  # in-place revise is the user's operation
    ]
    assert rejections[3].entry == "e9"
    events, _ = plan_state_ops(ops[-1:], FRAGMENTS, view, source="user")
    assert [(e["op"], e["entry_id"], e["text"], e["source"]) for e in events] == [("revise", "a", "edited", "user")]


def test_add_past_the_limit_is_rejected_but_a_retire_first_frees_a_slot():
    view = fold_events([_add("threads", str(i), f"thread {i}") for i in range(MAX_ACTIVE_ENTRIES)])
    _, rejections = plan_state_ops([StateOp("add", "threads", text="one more")], FRAGMENTS, view, source="agent")
    assert [r.reason for r in rejections] == ["full"]
    ops = [StateOp("retire", "threads", entry_id="0"), StateOp("add", "threads", text="one more")]
    events, rejections = plan_state_ops(ops, FRAGMENTS, view, source="agent", new_id=_ids())
    assert rejections == [] and [e["op"] for e in events] == ["retire", "add"]


def test_events_carry_label_mode_and_source_but_no_anchor():
    events, _ = plan_state_ops([StateOp("add", "threads", text="one")], FRAGMENTS, StateView(), source="agent")
    assert events[0]["fragment_label"] == "Threads"
    assert events[0]["mode"] == "entries" and events[0]["source"] == "agent"
    assert "message_id" not in events[0]


def test_carry_applies_adds_and_existing_entries_and_drops_the_rest():
    parent = fold_events([_add("threads", "old", "earlier")])
    carried = [
        {**_add("threads", "new", "user fact"), "source": "user"},
        {"fragment_id": "threads", "entry_id": "old", "op": "revise", "text": "earlier, fixed", "source": "user"},
        {"fragment_id": "threads", "entry_id": "from-discarded-reply", "op": "retire", "source": "user"},
    ]
    applied, dropped = carry_events(carried, parent)
    assert [e["entry_id"] for e in applied] == ["new", "old"]
    assert [e["entry_id"] for e in dropped] == ["from-discarded-reply"]
    assert [e.text for e in parent.active("threads")] == ["earlier, fixed", "user fact"]


# ── Configuration ────────────────────────────────────────────────────────────


def test_state_fragment_row_parses_with_defaults():
    fragment = state_fragment_of(
        {"id": "x", "label": "X", "field_type": "state", "state_mode": "bogus", "injection_label": "", "enabled": 0}
    )
    assert fragment is not None
    assert (fragment.mode, fragment.update, fragment.inject, fragment.heading, fragment.enabled) == (
        "value",
        "after_reply",
        "both",
        "X",
        False,
    )
    assert state_fragment_of({"id": "x", "label": "X", "field_type": "string"}) is None


def test_legacy_types_upgrade_to_explicit_state_settings():
    assert upgrade_legacy_fragment({"id": "t", "field_type": "progressive"}) == {
        "id": "t",
        "field_type": "state",
        "state_mode": "value",
        "state_update": "before_writer",
        "state_inject": "both",
    }
    upgraded = upgrade_legacy_fragment({"id": "n", "field_type": "direction_note", "direction_note_timing": "pre_writer"})
    assert (upgraded["state_mode"], upgraded["state_update"], "direction_note_timing" in upgraded) == (
        "entries",
        "before_writer",
        False,
    )
    assert upgrade_legacy_fragment({"id": "s", "field_type": "string"}) == {"id": "s", "field_type": "string"}


def _row(fid, mode, update, inject="both", enabled=1):
    return {
        "id": fid,
        "label": fid,
        "field_type": "state",
        "state_mode": mode,
        "state_update": update,
        "state_inject": inject,
        "enabled": enabled,
    }


def test_contract_routes_each_fragment_to_its_transport():
    rows = [
        _row("v_before", "value", "before_writer"),
        _row("e_before", "entries", "before_writer"),
        _row("v_after", "value", "after_reply"),
        _row("manual", "entries", "manual", inject="writer"),
        _row("off", "entries", "after_reply", enabled=0),
    ]
    contract = StateContract.capture({"enable_agent": 1, "state_updates": 1}, rows)
    assert [f.id for f in contract.director_values()] == ["v_before"]
    assert [f.id for f in contract.before_writer_tool()] == ["e_before"]
    assert [f.id for f in contract.after_reply()] == ["v_after"]
    assert [f.id for f in contract.to_writer()] == ["v_before", "e_before", "v_after", "manual"]
    # Updates off (switch or Agent): nothing routes, injection still does.
    for settings in ({"enable_agent": 1, "state_updates": 0}, {"enable_agent": 0, "state_updates": 1}):
        off = StateContract.capture(settings, rows)
        assert off.director_values() == off.tool_fragments() == ()
        assert len(off.to_writer()) == 4
    scene = {"id": "pacing", "field_type": "string"}
    assert [r["id"] for r in contract.direct_scene_rows([scene, *rows])] == ["pacing", "v_before"]


# ── Model-facing shape ───────────────────────────────────────────────────────


def test_state_tool_puts_retire_first_and_carries_no_volatile_state():
    tool = build_state_tool([VALUE, ENTRIES])
    params = tool["function"]["parameters"]
    assert list(params["properties"]) == ["retire", "place", "threads"]
    assert params["properties"]["place"]["type"] == "string"
    assert params["properties"]["threads"] == {"type": "array", "items": {"type": "string"}, "description": ENTRIES.description}
    assert params["required"] == []
    assert build_state_tool([VALUE, ENTRIES]) == tool


def test_request_lists_entries_under_aliases_numbered_across_the_request():
    other = StateFragment("clues", "Clues", "Clues", "Clues found.", mode="entries")
    view = fold_events([_add("threads", "t1", "find the key"), _add("clues", "c1", "a torn map"), _add("place", "p", "docks")])
    aliases = entry_aliases([VALUE, ENTRIES, other], view)
    assert [(a.alias, a.entry_id) for a in aliases] == [("e1", "t1"), ("e2", "c1")]
    request = build_state_request(
        [VALUE, ENTRIES, other],
        view,
        aliases,
        placement="before_writer",
        tool_schema=build_state_tool([VALUE, ENTRIES, other]),
        decision_guidance="**Decisions**\nThat action fails hard.",
        scene_direction="**Scene Guidance**\nKeep it tense.",
        user_message="I pick the lock.",
    )
    assert "Current value: docks" in request
    assert "- [e1] find the key" in request and "- [e2] a torn map" in request
    assert f"1 of {MAX_ACTIVE_ENTRIES} entries" in request
    # The resolved decision is context for the updater, not a schema field.
    assert "That action fails hard." in request
    assert "Keep it tense." in request and '"""I pick the lock."""' in request
    assert "t1" not in request and "c1" not in request


def test_parse_maps_aliases_back_and_orders_retires_first():
    view = fold_events([_add("threads", "t1", "find the key")])
    aliases = entry_aliases([ENTRIES], view)
    calls = [{"name": "update_state", "arguments": {"threads": ["new one"], "retire": "[E1]", "place": "docks", "other": "x"}}]
    ops, rejections = parse_state_call(calls, [VALUE, ENTRIES], aliases, known_ids=frozenset({"other"}))
    assert [(o.op, o.fragment_id, o.entry_id or o.text) for o in ops] == [
        ("retire", "threads", "t1"),
        ("add", "threads", "new one"),
        ("set", "place", "docks"),
    ]
    assert rejections == []  # "other" belongs to another call of the same turn


def test_parse_never_reinterprets_a_string_as_a_list():
    ops, rejections = parse_state_call(
        [{"name": "update_state", "arguments": {"threads": "one entry", "place": ["a"], "retire": {"x": 1}}}],
        [VALUE, ENTRIES],
        [],
    )
    assert ops == []
    assert sorted(r.reason for r in rejections) == ["malformed", "malformed", "malformed"]


def test_state_block_renders_values_lists_and_transitions():
    prior = fold_events([_add("place", "p", "docks")])
    view = fold_events([{"fragment_id": "place", "entry_id": "p", "op": "revise", "text": "roof"}], prior)
    view.apply(_add("threads", "t", "find the key"))
    block = render_state_block([VALUE, ENTRIES], view, prior=prior)
    assert block == "**Current State**\n\nPlace: docks -> roof\n\nOpen threads:\n- find the key"
    assert render_state_block([VALUE, ENTRIES], StateView()) == ""
    several = fold_events([_add("place", "a", "docks"), _add("place", "b", "roof")])
    assert render_state_block([VALUE], several) == "**Current State**\n\nPlace:\n- docks\n- roof"
