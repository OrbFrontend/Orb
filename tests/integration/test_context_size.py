from backend.database import add_message, create_lorebook_entry, create_world, set_active_leaf, update_settings

# Persona fields are flat top-level keys on CharacterCardCreate (main.py). An earlier version of this test nested them under
# data={"spec":...,"data":{...}}, which Pydantic silently dropped -- the card was created name-only and the breakdown never saw
# the persona. These constants are asserted against the breakdown below so a regression that drops them fails loudly.
DESCRIPTION = (
    "A test character with a detailed persona for context size testing. "
    "She is curious, witty, and observant. She enjoys long conversations "
    "about philosophy and technology."
)
PERSONALITY = "Curious, witty, observant. Enjoys philosophical discussions and has a dry sense of humor."
SCENARIO = (
    "TestChar and the user are sitting in a quiet cafe, having a deep conversation about the nature of artificial intelligence."
)
FIRST_MES = "Hello! I see you're reading about neural networks too. What brings you here?"


def _rows(data: dict) -> dict[str, dict]:
    return {row["kind"]: row for row in data["breakdown"] if row["kind"] != "custom"}


async def test_context_size_measures_each_layout_block_as_the_writer_receives_it(client, llm_mock):
    """The estimate lists the active layout's blocks in order with the history between the regions, and the prefix rows add up
    to the system message the Writer is sent."""
    card_id = await client.create(
        "/api/characters",
        json={
            "name": "TestChar",
            "description": DESCRIPTION,
            "personality": PERSONALITY,
            "scenario": SCENARIO,
            "first_mes": FIRST_MES,
            "system_prompt": "Card rules. {{original}}",
        },
    )
    cid = await client.create("/api/conversations", json={"character_card_id": card_id})
    active_id = (await client.get_json("/api/prompt-layouts"))["active_id"]
    blocks = await client.get_json(f"/api/prompt-layouts/{active_id}/blocks")
    data = await client.get_json(f"/api/conversations/{cid}/context-size")

    assert data["message_count"] == 1
    rows = data["breakdown"]
    regions = [row["region"] for row in rows]
    assert regions == sorted(regions, key=["prefix", "history", "writer"].index)
    assert [row["id"] for row in rows if row["region"] != "history"] == [block["id"] for block in blocks if block["enabled"]]
    assert _rows(data)["history"]["chars"] == len(FIRST_MES)
    assert data["total_chars"] == sum(row["chars"] for row in rows)

    llm_mock.enqueue_writer("Hi.")
    _ = (await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Hey.", "attachments": []})).text
    system = next(call for call in llm_mock.captured if call["pass"] == "writer")["messages"][0]
    billed = [row["chars"] for row in rows if row["region"] == "prefix" and row["chars"]]
    assert system["role"] == "system"
    assert len(system["content"]) == sum(billed) + 2 * (len(billed) - 1)


async def test_context_size_404_for_missing(client):
    """Context size returns 404 for non-existent conversation."""
    await client.get_checked("/api/conversations/nonexistent/context-size", expected_status=404)


async def test_context_size_counts_prompt_rendered_message(client):
    replacement = "An expanded greeting the model actually receives."
    card = await client.post_json(
        "/api/characters",
        json={
            "name": "Scripted",
            "first_mes": "secret",
            "extensions": {
                "regex_scripts": [
                    {"findRegex": "/secret/g", "replaceString": replacement, "placement": [2], "promptOnly": True}
                ]
            },
        },
    )
    cid = await client.create("/api/conversations", json={"character_card_id": card["id"]})

    response = await client.get_json(f"/api/conversations/{cid}/context-size")

    assert _rows(response)["history"]["chars"] == len(replacement)


async def test_agentic_lorebook_bills_only_what_the_writer_can_receive(client):
    """Agentic lorebook bills no non-constant entries before the Director picks, even when the latest message matches."""
    world = await create_world({"name": "Armory", "is_global": True})
    await create_lorebook_entry(world["id"], {"name": "Sword", "content": "A legendary blade.", "keywords": ["sword"]})
    cid = await client.create("/api/conversations", json={"title": "Armory"})
    parent = None
    for role, text in [("user", "Bring the sword."), ("assistant", "Here is the sword.")]:
        parent, _ = await add_message(cid, role, text, 0, parent_id=parent)
    await set_active_leaf(cid, parent)

    substring = await client.get_json(f"/api/conversations/{cid}/context-size")
    await update_settings({"agentic_lorebook_enabled": 1, "enable_agent": 1})
    agentic = await client.get_json(f"/api/conversations/{cid}/context-size")

    assert _rows(substring)["selected_lore"]["chars"] > 0
    assert _rows(agentic)["selected_lore"]["chars"] == 0
