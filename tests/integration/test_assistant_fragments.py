"""The assistant's fragment tools: creates through the routes' own checks, setters that write only what differs, and text
edits and deletes that wait for Apply, a decision's outcomes included."""

import pytest

from backend.core import parse_decision_definition
from backend.database import get_interactive_fragment, get_mood_fragment
from backend.features.assistant import ToolFailure
from tests.integration.test_assistant import _apply, _propose, _tools, _use

REVISE = "/api/assistant/revise"
UNDO = "/api/assistant/undo"


async def _write(tools, name, **arguments):
    output = await tools[name].run(tools[name].arguments(**arguments))
    return output


async def test_a_mood_is_created_set_edited_and_deleted(client, db):
    tools = await _tools(mode="library")
    terse = {
        "label": "Terse",
        "description": "A tense scene that wants clipped prose.",
        "prompt_text": "Write short, clipped sentences.",
    }
    created = await _write(tools, "create_mood_fragment", **terse)
    assert created.committed == ({"kind": "mood_fragment", "fragment_id": "terse"},)
    again = await _write(tools, "create_mood_fragment", **terse)
    assert again.ids["fragment_id"] == "terse_2", "a create always adds, numbered past a taken id"
    listed, _ = await _use(tools, "list_fragments", kind="mood", query="terse")
    assert [row["id"] for row in listed["rows"]] == ["terse", "terse_2"]

    off = await _write(tools, "set_mood_fragment", fragment_id="terse", enabled="off", cooldown_turns=3)
    assert off.data["changed"] == ["cooldown_turns", "enabled"]
    stored = await get_mood_fragment("terse")
    assert stored is not None and (stored["enabled"], stored["cooldown_turns"]) == (0, 3)
    same = await _write(tools, "set_mood_fragment", fragment_id="terse", enabled="off")
    assert same.committed == () and same.data["changed"] == []

    edit = {"fragment_id": "terse", "field": "prompt_text", "mode": "find_replace", "find": "short", "text": "very short"}
    proposal = await _propose(tools, "edit_mood_fragment", **edit)
    assert (await get_mood_fragment("terse"))["prompt_text"] == "Write short, clipped sentences.", "nothing written yet"
    [applied] = await _apply(client, proposal["proposal_id"])
    assert applied["verdict"] == "applied" and applied["resources"] == [{"kind": "mood_fragment", "fragment_id": "terse"}]
    assert (await get_mood_fragment("terse"))["prompt_text"] == "Write very short, clipped sentences."
    [undone] = (await client.post_json(UNDO, json={"proposal_ids": [proposal["proposal_id"]]}))["results"]
    assert undone["verdict"] == "undone"
    assert (await get_mood_fragment("terse"))["prompt_text"] == "Write short, clipped sentences."

    # The stored text moves after the preview: Apply refuses rather than writing over it.
    await client.put_checked("/api/fragments/terse", json={"prompt_text": "Edited in the editor."})
    [stale] = await _apply(client, proposal["proposal_id"])
    assert stale["verdict"] == "stale"
    with pytest.raises(ToolFailure, match="cannot be left empty"):
        await _propose(tools, "edit_mood_fragment", fragment_id="terse", field="label", mode="replace_all", text=" ")

    # With the Director off the mood would do nothing; the result says so, and switches nothing.
    await _write(tools, "set_pipeline_toggle", toggle="director", enabled=False)
    quiet = await _write(tools, "create_mood_fragment", **{**terse, "label": "Quiet"})
    assert "note" in quiet.data and quiet.committed == ({"kind": "mood_fragment", "fragment_id": "quiet"},)
    assert "note" not in (await _write(tools, "create_mood_fragment", **{**terse, "label": "Off", "enabled": False})).data

    deleted = await _propose(tools, "delete_mood_fragment", fragment_id="terse_2")
    assert await get_mood_fragment("terse_2") is not None
    assert (await _apply(client, deleted["proposal_id"]))[0]["verdict"] == "applied"
    assert await get_mood_fragment("terse_2") is None


async def test_interactive_settings_keep_to_their_type_and_a_move_keeps_its_stage(client):
    tools = await _tools(mode="library")
    with pytest.raises(ToolFailure, match="required applies only"):
        await _write(
            tools,
            "create_interactive_fragment",
            label="Polish",
            field_type="post_processing",
            description="Trim.",
            required=True,
        )
    with pytest.raises(ToolFailure, match="state fragment only"):
        await _write(
            tools, "create_interactive_fragment", label="Mood", field_type="single", description="x", state_mode="entries"
        )

    pacing = await _write(tools, "create_interactive_fragment", label="Pacing", field_type="single", description="slow or fast")
    assert pacing.ids["fragment_id"] == "pacing"
    director = [
        row["id"]
        for row in (await _use(tools, "list_fragments", kind="interactive", limit=30))[0]["rows"]
        if row["stage"] == "director"
    ]
    assert director[-1] == "pacing", "a new fragment runs last in its stage"
    stored = await get_interactive_fragment("pacing")
    assert stored is not None and stored["injection_label"] == "Pacing"

    moved = await _write(tools, "set_interactive_fragment", fragment_id="pacing", position=1)
    assert moved.data["changed"] == ["position"] and moved.data["fragment"]["position"] == 1
    listed = (await _use(tools, "list_fragments", kind="interactive", limit=30))[0]["rows"]
    assert [row["id"] for row in listed if row["stage"] == "director"] == ["pacing", *director[:-1]]

    # Required means nothing for a state value updated after the reply: the type change clears it.
    await _write(tools, "set_interactive_fragment", fragment_id="pacing", required="on")
    state = await _write(tools, "set_interactive_fragment", fragment_id="pacing", field_type="state")
    stored = await get_interactive_fragment("pacing")
    assert stored is not None and (stored["field_type"], stored["state_mode"], stored["state_update"]) == (
        "state",
        "value",
        "after_reply",
    )
    assert stored["required"] == 0 and "note_required" in state.data
    with pytest.raises(ToolFailure, match="keeps its type"):
        await _write(tools, "set_interactive_fragment", fragment_id="outcome", field_type="single")
    with pytest.raises(ToolFailure, match="belongs to another type"):
        await _propose(tools, "edit_interactive_fragment", fragment_id="pacing", field="question", mode="replace_all", text="?")

    deleted = await _propose(tools, "delete_interactive_fragment", fragment_id="pacing")
    assert (await _apply(client, deleted["proposal_id"]))[0]["verdict"] == "applied"
    assert await get_interactive_fragment("pacing") is None


async def test_a_decision_is_created_whole_and_one_outcome_is_edited_under_its_column(client):
    tools = await _tools(mode="library")
    with pytest.raises(ToolFailure, match="one per meaning"):
        await _write(
            tools,
            "create_decision_fragment",
            label="Mood swing",
            decision_type="choice",
            question="Which way does the mood turn?",
            outcomes=["calm"],
            outcome_meanings=["It settles.", "It sours."],
        )
    created = await _write(
        tools,
        "create_decision_fragment",
        label="Luck",
        decision_type="yes_no",
        question="Is luck on their side?",
        outcome_meanings=["Fortune helps.", "Fortune hinders."],
    )
    row = await get_interactive_fragment(created.ids["fragment_id"])
    assert row is not None and parse_decision_definition(row) is not None, "it runs as the editor would have made it"
    assert row["decision_outputs"] == {"true": "Fortune helps.", "false": "Fortune hinders."}
    assert (row["decision_resolution"], row["decision_threshold"]) == ("threshold", 0.5)

    rolled = await _write(tools, "set_decision_fragment", fragment_id="luck", resolution="roll")
    assert sorted(rolled.data["changed"]) == ["decision_resolution", "decision_threshold"]
    assert (await get_interactive_fragment("luck"))["decision_threshold"] is None
    with pytest.raises(ToolFailure, match="only for the threshold"):
        await _write(tools, "set_decision_fragment", fragment_id="luck", threshold_percent=70)

    read, _ = await _use(tools, "read_fragment", kind="interactive", fragment_id="luck")
    assert [item["outcome"] for item in read["decision"]["outcomes"]] == ["true", "false"]
    edit = {"fragment_id": "luck", "field": "outcome_guidance", "outcome": "false", "mode": "replace_all", "text": "A snag."}
    proposal = await _propose(tools, "edit_interactive_fragment", **edit)
    assert (await _apply(client, proposal["proposal_id"]))[0]["verdict"] == "applied"
    row = await get_interactive_fragment("luck")
    assert row["decision_outputs"] == {"true": "Fortune helps.", "false": "A snag."}, "only that outcome changed"

    # Another outcome changed after the preview: it is kept, and the edit lands on its own outcome.
    again = await _propose(tools, "edit_interactive_fragment", **{**edit, "text": "A bigger snag."})
    await client.put_checked(
        "/api/interactive-fragments/luck", json={"decision_outputs": {"true": "Luck holds.", "false": "A snag."}}
    )
    assert (await _apply(client, again["proposal_id"]))[0]["verdict"] == "applied"
    assert (await get_interactive_fragment("luck"))["decision_outputs"] == {"true": "Luck holds.", "false": "A bigger snag."}
    # Its own outcome changed after the preview: that is stale.
    third = await _propose(tools, "edit_interactive_fragment", **{**edit, "find": "", "text": "No snag."})
    await client.put_checked(
        "/api/interactive-fragments/luck", json={"decision_outputs": {"true": "Luck holds.", "false": "Edited."}}
    )
    assert (await _apply(client, third["proposal_id"]))[0]["verdict"] == "stale"

    meaning = await _propose(
        tools,
        "edit_interactive_fragment",
        fragment_id="luck",
        field="outcome_meaning",
        outcome="true",
        mode="replace_all",
        text="Fortune smiles.",
    )
    revised = await client.post_json(REVISE, json={"proposal_id": meaning["proposal_id"], "text": "  "})
    assert revised["verdict"] == "refused"
    with pytest.raises(ToolFailure, match="Name one outcome"):
        await _propose(tools, "edit_interactive_fragment", **{**edit, "outcome": "maybe"})
