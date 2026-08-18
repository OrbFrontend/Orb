"""Byte-parity: the off-turn prefix equals the pipeline's turn prefix.

Off-turn workflow calls (image_gen's analyze/compose, and anything else built
on ``build_offturn_prefix``) ride the llama.cpp server's cached KV for the
whole conversation prefix. That only works if the toolkit builder and the
pipeline's ``_build_prefixes`` produce **byte-identical** messages for the same
conversation state — one diverging byte evicts the cache for the off-turn call
and again for the next chat turn. This test seeds every prefix-shaping input
(card-bound conversation, active persona, macros, post-history instructions,
constant + keyword lorebook entries) and compares the two builders' output
serialized, which is exactly the equality the server's prefix matcher sees.
"""

from __future__ import annotations

import json

import pytest

from backend.database import (
    add_message,
    create_character_card,
    create_conversation,
    create_lorebook_entry,
    create_user_persona,
    create_world,
    get_messages,
    get_settings,
    set_active_leaf,
    update_settings,
)
from backend.pipeline.context import _build_prefixes, _load_pipeline_context
from backend.workflows.toolkit import build_offturn_prefix


def _serialize(prefix) -> str:
    return "\n".join(json.dumps(m, separators=(",", ":"), sort_keys=True) for m in prefix)


@pytest.mark.asyncio
async def test_offturn_prefix_is_byte_identical_to_pipeline_prefix(client):
    conv_id = "prefix-parity"
    await create_character_card(
        {
            "id": "parity-char",
            "name": "Iris",
            "description": "Iris is a tired librarian.",
            "personality": "Dry, patient.",
            "scenario": "A rainy archive.",
            "mes_example": "<START>\n{{char}}: Shelve it yourself.",
            "system_prompt": "You are {{char}}, speaking with {{user}}.",
            "post_history_instructions": "Stay in character.",
        }
    )
    persona = await create_user_persona({"name": "Chi", "description": "A curious visitor."})
    await update_settings({"active_persona_id": persona["id"]})
    world = await create_world({"name": "Archive"})
    await create_lorebook_entry(
        world["id"],
        {"name": "Canon", "content": "The moon is shattered.", "constant": True},
    )
    await create_lorebook_entry(
        world["id"],
        {"name": "Sword", "content": "A legendary blade.", "keywords": ["sword"]},
    )
    await create_conversation(conv_id, "Parity", "Iris", "A rainy archive.", character_card_id="parity-char")
    mid, _ = await add_message(conv_id, "user", "Hello there.", 0)
    mid, _ = await add_message(conv_id, "assistant", "She looks up from the desk.", 0, parent_id=mid)
    await set_active_leaf(conv_id, mid)

    settings = await get_settings()
    history = await get_messages(conv_id)

    ctx = await _load_pipeline_context(conv_id)
    assert ctx is not None
    pipeline_prefix, _ = _build_prefixes(ctx, history)
    offturn_prefix = await build_offturn_prefix(conv_id, history, settings)
    single_agent_prefix = await build_offturn_prefix(conv_id, history, settings, lane="agent")

    # Guard against a vacuous pass: the fixture must actually exercise the
    # constant-lorebook and persona sections of the system body.
    body = pipeline_prefix[0]["content"]
    assert "## Lorebook" in body and "The moon is shattered." in body
    assert "A legendary blade." not in body
    assert "A curious visitor." in body

    assert _serialize(offturn_prefix) == _serialize(pipeline_prefix)
    assert _serialize(single_agent_prefix) == _serialize(pipeline_prefix)

    # Dual-model mode substitutes only the agent system prompt; every other
    # prefix-shaping byte must still match the pipeline's own agent builder.
    endpoint = await client.post("/api/endpoints", json={"url": "http://agent.local", "api_key": "agent-key"})
    assert endpoint.status_code == 200
    await update_settings(
        {
            "agent_same_as_writer": 0,
            "agent_endpoint_id": endpoint.json()["id"],
            "agent_shared_system_prompt": "Agent-only system prompt.",
            "prevent_prompt_overrides": 1,
        }
    )
    dual_settings = await get_settings()
    dual_ctx = await _load_pipeline_context(conv_id)
    assert dual_ctx is not None
    dual_writer_prefix, dual_agent_prefix = _build_prefixes(dual_ctx, history)
    assert dual_agent_prefix is not None
    offturn_agent_prefix = await build_offturn_prefix(conv_id, history, dual_settings, lane="agent")

    assert _serialize(offturn_agent_prefix) == _serialize(dual_agent_prefix)
    assert _serialize(offturn_agent_prefix) != _serialize(dual_writer_prefix)
    assert offturn_agent_prefix[0]["content"].startswith("Agent-only system prompt.")


@pytest.mark.parametrize("context_mode", ["private", "shared", "swap"])
@pytest.mark.asyncio
async def test_offturn_prefix_matches_a_group_turn_prefix(client, context_mode):
    """A group's prefix is a different document: the cast section stands in for
    the card, {{char}} is the scene title, {{cast}} is the roster, and every
    assistant line is attributed to the member who wrote it. An off-turn builder
    that rebuilt the solo shape would evict the conversation's KV on every
    workflow call — and hand image_gen a transcript with nobody's name on it.

    The neutral base (no active speaker) is the comparison in all three modes:
    it is the base the Director runs on, and under Classic card swap it is the
    only one an off-turn call can name without picking a speaker for itself.
    """
    aria = await client.post("/api/characters", json={"name": "Aria", "description": "A tired ranger."})
    kael = await client.post("/api/characters", json={"name": "Kael", "description": "A blunt smith."})
    conv = await client.post(
        "/api/conversations",
        json={
            "kind": "group",
            "title": "The Long Watch",
            "group_context_mode": context_mode,
            "members": [{"character_card_id": aria.json()["id"]}, {"character_card_id": kael.json()["id"]}],
        },
    )
    conv_id = conv.json()["id"]
    members = (await client.get(f"/api/conversations/{conv_id}/members")).json()
    mid, _ = await add_message(conv_id, "user", "What was that noise?", 0)
    mid, _ = await add_message(conv_id, "assistant", "Aria lifts the lantern.", 1, parent_id=mid, speaker_member_id=members[0]["id"])
    await set_active_leaf(conv_id, mid)

    settings = await get_settings()
    history = await get_messages(conv_id)
    ctx = await _load_pipeline_context(conv_id)
    assert ctx is not None
    pipeline_prefix, _ = _build_prefixes(ctx, history)

    # Guard against a vacuous pass: the fixture must exercise the group shape.
    body = pipeline_prefix[0]["content"]
    assert "## Cast" in body
    assert "## Character: The Long Watch" not in body
    assert {"role": "assistant", "content": "Aria: Aria lifts the lantern."} in pipeline_prefix

    assert _serialize(await build_offturn_prefix(conv_id, history, settings)) == _serialize(pipeline_prefix)
    assert _serialize(await build_offturn_prefix(conv_id, history, settings, lane="agent")) == _serialize(pipeline_prefix)
