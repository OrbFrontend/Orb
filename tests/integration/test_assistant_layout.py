"""The assistant's prompt layout tools: creates and setters commit at once on the active profile, text edits and deletes
wait for Apply and write the profile they were proposed in, and built-in blocks keep the layout's own rules."""

import pytest

from backend.database import get_prompt_block, get_prompt_blocks
from backend.features.assistant import ToolFailure
from tests.integration.test_assistant import _apply, _propose, _tools, _use

UNDO = "/api/assistant/undo"


async def _run(tools, name, **arguments):
    return await tools[name].run(tools[name].arguments(**arguments))


async def test_a_block_is_created_placed_edited_and_deleted_in_the_profile_it_was_proposed_in(client):
    tools = await _tools(mode="library")
    listed, _ = await _use(tools, "list_prompt_blocks")
    prefix = [row["id"] for row in listed["prefix"]]
    assert prefix[0] == "main_prompt" and [row["id"] for row in listed["writer"]][-1] == "depth_lore"

    created = await _run(
        tools, "create_prompt_block", label="Style", content="Write in {{char}}'s voice.", region="prefix", position=2
    )
    block_id = created.ids["block_id"]
    profile_id = created.ids["profile_id"]
    assert created.committed == ({"kind": "prompt_block", "profile_id": profile_id, "block_id": block_id},)
    rows = await get_prompt_blocks(profile_id)
    assert [row["id"] for row in rows if row["region"] == "prefix"][:3] == ["main_prompt", block_id, prefix[1]]

    # Crossing Chat history with the default role takes the writer side's; asking again changes nothing.
    moved = await _run(tools, "set_prompt_block", block_id=block_id, region="writer", position=1, enabled="off")
    assert moved.data["changed"] == ["region", "enabled", "role"] and moved.data["role"] == "user"
    again = await _run(tools, "set_prompt_block", block_id=block_id, region="writer", position=1, enabled="off")
    assert again.committed == () and again.data["changed"] == []
    with pytest.raises(ToolFailure, match="stays on its side"):
        await _run(tools, "set_prompt_block", block_id="user_message", region="prefix")

    # A built-in's text keeps its placeholder, and a built-in cannot be deleted.
    with pytest.raises(ToolFailure, match=r"must keep \{\{scene_guidance\}\}"):
        await _propose(tools, "edit_prompt_block", block_id="scene_guidance", field="content", mode="replace_all", text="x")
    with pytest.raises(ToolFailure, match="cannot be deleted"):
        await _propose(tools, "delete_prompt_block", block_id="main_prompt")

    edit = {"block_id": block_id, "field": "content", "mode": "find_replace", "find": "voice", "text": "own voice"}
    proposal = await _propose(tools, "edit_prompt_block", **edit)
    removal = await _propose(tools, "delete_prompt_block", block_id=block_id)
    assert (await get_prompt_block(block_id, profile_id))["content"] == "Write in {{char}}'s voice.", "nothing written yet"

    # Another profile becomes active: Apply still writes the block it previewed, in its own profile.
    other = (await client.post_json("/api/profiles", json={"name": "Other"}))["id"]
    await client.put_checked("/api/profiles/active", json={"id": other})
    [applied] = await _apply(client, proposal["proposal_id"])
    assert applied["verdict"] == "applied"
    assert (await get_prompt_block(block_id, profile_id))["content"] == "Write in {{char}}'s own voice."
    [undone] = (await client.post_json(UNDO, json={"proposal_ids": [proposal["proposal_id"]]}))["results"]
    assert undone["verdict"] == "undone"

    # The text moves after the preview: Apply refuses rather than writing over it.
    await client.put_checked("/api/profiles/active", json={"id": profile_id})
    edited = await client.patch(f"/api/prompt-blocks/{block_id}", json={"content": "Edited by hand."})
    assert edited.status_code == 200
    [stale] = await _apply(client, proposal["proposal_id"])
    assert stale["verdict"] == "stale"

    [deleted] = await _apply(client, removal["proposal_id"])
    assert deleted["verdict"] == "applied" and await get_prompt_block(block_id, profile_id) is None
