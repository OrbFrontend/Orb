"""Agentic activation uses only the Director's picks through solo and group turns."""

import pytest

from backend.database import create_lorebook_entry, create_world, update_settings


async def _scene(client, kind):
    world = await create_world({"name": "World", "is_global": True})
    for entry in (
        {"name": "Dragon", "content": "The dragon guards a hidden hoard."},
        {"name": "Natlan", "content": "The city lies beyond the volcano.", "keywords": ["natlan"]},
        {"name": "Canon", "content": "The moon is shattered.", "constant": 1},
        {"name": "Rules", "content": "Append a health bar.", "constant": 1, "at_depth": 1},
    ):
        await create_lorebook_entry(world["id"], entry)
    card_id = await client.create("/api/characters", json={"name": "Aria", "first_mes": "The path is quiet."})
    body = (
        {"character_card_id": card_id}
        if kind == "solo"
        else {"kind": "group", "group_turn_mode": "round_robin", "members": [{"character_card_id": card_id}]}
    )
    return await client.create("/api/conversations", json=body)


@pytest.mark.parametrize("kind", ["solo", "group"])
@pytest.mark.parametrize("selection", ["picked", "empty", "failed"])
async def test_agent_picks_replace_keyword_activation(client, llm_mock, kind, selection):
    await update_settings({"agentic_lorebook_enabled": 1, "enable_agent": 1, "enabled_tools": {}})
    cid = await _scene(client, kind)
    if selection == "failed":
        llm_mock.fail("workflow", RuntimeError("Selection unavailable"))
    else:
        llm_mock.enqueue_workflow(
            {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": "select_lorebook",
                            "arguments": {"selected_lorebook_entries": ["Dragon"] if selection == "picked" else []},
                        },
                    }
                ]
            }
        )
    llm_mock.enqueue_writer("We set out.")

    response = await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "We travel to Natlan."})

    assert "event: done" in response.text
    selectors = [
        call
        for call in llm_mock.captured
        if isinstance(call["tool_choice"], dict) and call["tool_choice"].get("function", {}).get("name") == "select_lorebook"
    ]
    assert len(selectors) == 1
    selector = selectors[0]["messages"]
    # Constants stay out of the pick catalog, but the call shares the conversation prefix that carries them.
    assert "Canon" not in selector[-1]["content"]
    assert "The moon is shattered." in selector[0]["content"]
    assert "Append a health bar." not in selector[-1]["content"]
    writers = [call for call in llm_mock.captured if call["pass"] == "writer"]
    assert len(writers) == 1
    writer = writers[0]
    tail = writer["messages"][-1]["content"]
    assert ("The dragon guards a hidden hoard." in tail) == (selection == "picked")
    assert "The city lies beyond the volcano." not in tail
    assert "The moon is shattered." in writer["messages"][0]["content"]
    assert "Append a health bar." in tail


@pytest.mark.parametrize("settings", [{"agentic_lorebook_enabled": 0}, {"agentic_lorebook_enabled": 1, "enable_agent": 0}])
async def test_disabling_agentic_mode_restores_keyword_activation(client, llm_mock, settings):
    await update_settings({**settings, "enabled_tools": {}})
    cid = await _scene(client, "solo")
    llm_mock.enqueue_writer("We set out.")

    await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "We travel to Natlan."})

    writer = next(call for call in llm_mock.captured if call["pass"] == "writer")
    assert "The city lies beyond the volcano." in writer["messages"][-1]["content"]
    assert not any(
        isinstance(call["tool_choice"], dict) and call["tool_choice"].get("function", {}).get("name") == "select_lorebook"
        for call in llm_mock.captured
    )


@pytest.mark.parametrize("separate_agent", [False, True])
async def test_selection_extends_the_director_prefix_and_omits_constants_from_its_catalog(client, llm_mock, separate_agent):
    await update_settings(
        {"agentic_lorebook_enabled": 1, "enable_agent": 1, "enabled_tools": {"direct_scene": True, "editor_apply_patch": True}}
    )
    if separate_agent:
        endpoint_id = await client.create("/api/endpoints", json={"name": "Agent", "url": "http://agent.local/v1"})
        await client.put_checked(
            "/api/settings",
            json={
                "agent_same_as_writer": False,
                "agent_endpoint_id": endpoint_id,
                "agent_shared_system_prompt": "Coordinate the scene.",
            },
        )
    cid = await _scene(client, "solo")

    for text in ("We travel to Natlan.", "We continue through Natlan."):
        llm_mock.enqueue_workflow(
            {
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "select_lorebook", "arguments": {"selected_lorebook_entries": []}},
                    }
                ]
            }
        )
        # A seeded banned phrase makes the Editor call on both turns.
        llm_mock.enqueue_writer("A mix of shadows and mist surrounds us.")
        await client.post_checked(f"/api/conversations/{cid}/send", json={"content": text})

    selectors = [call for call in llm_mock.captured if call["pass"] == "workflow"]
    assert len(selectors) == 2
    assert selectors[0]["messages"][0] == selectors[1]["messages"][0]
    assert selectors[0]["tools"] == selectors[1]["tools"]
    for selector in selectors:
        assert "The moon is shattered." not in selector["messages"][-1]["content"]
        assert "Append a health bar." not in selector["messages"][-1]["content"]

    directors = [call for call in llm_mock.captured if call["pass"] == "director"]
    editors = [call for call in llm_mock.captured if call["pass"] == "editor"]
    writers = [call for call in llm_mock.captured if call["pass"] == "writer"]
    assert len(directors) == len(editors) == len(writers) == 2
    for selector, director, editor, writer in zip(selectors, directors, editors, writers, strict=True):
        assert selector["endpoint"] == director["endpoint"]
        assert selector["model"] == director["model"]
        assert selector["tools"] == director["tools"]
        assert selector["messages"][:-1] == director["messages"][:-1]
        assert director["messages"][0] == editor["messages"][0]
        if separate_agent:
            assert "Coordinate the scene." in selector["messages"][0]["content"]
        else:
            assert editor["messages"][0] == writer["messages"][0]
        assert "The moon is shattered." in writer["messages"][0]["content"]
        assert editor["messages"][1 : len(writer["messages"])] == writer["messages"][1:]
        assert "Append a health bar." in writer["messages"][-1]["content"]
