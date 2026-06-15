"""Test GET /api/conversations/{cid}/context-size"""

# Persona fields are flat top-level keys on CharacterCardCreate (main.py).
# An earlier version of this test nested them under data={"spec":...,"data":{...}},
# which Pydantic silently dropped -- the card was created name-only and the
# breakdown never saw the persona. These constants are asserted against the
# breakdown below so a regression that drops them fails loudly.
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


async def test_context_size_returns_breakdown(client):
    """Context size endpoint returns token estimates per component.

    Drives the persona through the real card -> conversation path: the card's
    description+personality become ``char_persona``, its scenario becomes
    ``scenario``, and its first_mes is seeded as the opening assistant message,
    so the breakdown's char counts must equal those source strings exactly.
    """
    card_resp = await client.post(
        "/api/characters",
        json={
            "name": "TestChar",
            "description": DESCRIPTION,
            "personality": PERSONALITY,
            "scenario": SCENARIO,
            "first_mes": FIRST_MES,
        },
    )
    assert card_resp.status_code == 200
    card_id = card_resp.json()["id"]

    # Create the conversation purely from the card so every persona field the
    # breakdown reports is sourced from the card, not from conversation-level
    # overrides.
    resp = await client.post(
        "/api/conversations",
        json={"character_card_id": card_id},
    )
    assert resp.status_code == 200
    cid = resp.json()["id"]

    # Get context size
    resp = await client.get(f"/api/conversations/{cid}/context-size")
    assert resp.status_code == 200
    data = resp.json()

    # Verify structure
    assert "total_tokens_est" in data
    assert "total_chars" in data
    assert "breakdown" in data
    assert "message_count" in data
    # first_mes is auto-seeded as the opening assistant message.
    assert data["message_count"] == 1

    # Verify breakdown components
    bd = data["breakdown"]
    expected_keys = {
        "system_prompt",
        "char_persona",
        "scenario",
        "mes_example",
        "user_persona",
        "messages",
        "post_history",
        "director_injection",
        "lorebook",
    }
    assert set(bd.keys()) == expected_keys

    # Each component has chars and tokens_est
    for _, val in bd.items():
        assert "chars" in val
        assert "tokens_est" in val
        assert isinstance(val["chars"], int)
        assert isinstance(val["tokens_est"], int)

    # The persona fields must actually flow into the breakdown. char_persona is
    # description and personality joined by a blank line (resolve_char_context);
    # scenario and the seeded first_mes pass through verbatim.
    assert bd["char_persona"]["chars"] == len(f"{DESCRIPTION}\n\n{PERSONALITY}")
    assert bd["scenario"]["chars"] == len(SCENARIO)
    assert bd["messages"]["chars"] == len(FIRST_MES)

    # Total should be positive
    assert data["total_tokens_est"] > 0
    assert data["total_chars"] > 0


async def test_context_size_404_for_missing(client):
    """Context size returns 404 for non-existent conversation."""
    resp = await client.get("/api/conversations/nonexistent/context-size")
    assert resp.status_code == 404
