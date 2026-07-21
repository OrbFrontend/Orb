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

    # Guard against a vacuous pass: the fixture must actually exercise the
    # constant-lorebook and persona sections of the system body.
    body = pipeline_prefix[0]["content"]
    assert "## Lorebook" in body and "The moon is shattered." in body
    assert "A legendary blade." not in body
    assert "A curious visitor." in body

    assert _serialize(offturn_prefix) == _serialize(pipeline_prefix)
