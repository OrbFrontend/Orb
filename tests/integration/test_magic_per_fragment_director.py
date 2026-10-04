"""magic_rewrite composes with the per-fragment director mode.

With ``director_individual_fragments`` on, the director issues one forced ``direct_scene`` call per interactive fragment instead
of a single combined call. magic_rewrite routes through that same director pass, so the user's direction reaches every
per-fragment call and the rewrite still lands as a new sibling.
"""

from __future__ import annotations

import json

from backend.database import get_message_by_id, get_messages, set_workflow_enabled

_DIRECTION = "make the ranger vanish into mist"


async def _seed_reply(client, llm_mock) -> tuple[str, int]:
    """Open a conversation with per-fragment director on and one interactive
    fragment, then produce one assistant reply; return (cid, reply id)."""
    card = await client.post_json(
        "/api/characters", json={"name": "Aria", "description": "An elf ranger.", "first_mes": "The woods are quiet."}
    )
    cid = await client.create("/api/conversations", json={"character_card_id": card["id"]})

    # An explicit interactive fragment guarantees the per-fragment branch engages
    # regardless of which fragments ship in the default seed.
    await client.post_checked(
        "/api/interactive-fragments",
        json={
            "id": "test_pacing",
            "label": "Pacing",
            "description": "scene pacing",
            "field_type": "string",
            "required": False,
            "enabled": True,
            "injection_label": "Pacing",
            "sort_order": 50,
        },
    )

    await client.put_checked(
        "/api/settings",
        json={
            "model_name": "writer-model",
            "enable_agent": True,
            "enabled_tools": {"direct_scene": True},
            "director_individual_fragments": True,
        },
    )
    # Keep the seeded and rewritten contents exact; the normalizer is irrelevant here.
    await set_workflow_enabled("format_consistency", False)

    llm_mock.enqueue_writer("The original reply.")
    send = await client.post_checked(f"/api/conversations/{cid}/send", json={"content": "Tell me a story.", "attachments": []})
    _ = send.text

    original = [m for m in await get_messages(cid) if m["role"] == "assistant"][-1]
    return cid, original["id"]


async def test_magic_rewrite_drives_the_per_fragment_director(client, llm_mock):
    cid, original_id = await _seed_reply(client, llm_mock)
    original = await get_message_by_id(original_id)
    assert original is not None

    llm_mock.enqueue_writer("A storm-soaked rewrite.")
    start = len(llm_mock.captured)
    resp = await client.post_checked(
        f"/api/conversations/{cid}/messages/{original_id}/magic_rewrite", json={"direction": _DIRECTION}
    )
    _ = resp.text
    captured = llm_mock.captured[start:]

    # magic still lands a new sibling under per-fragment mode.
    sibling = [m for m in await get_messages(cid) if m["role"] == "assistant"][-1]
    assert sibling["id"] != original_id
    sibling_row = await get_message_by_id(sibling["id"])
    assert sibling_row is not None
    assert sibling_row["turn_index"] == original["turn_index"]
    assert sibling_row["parent_id"] == original["parent_id"]
    assert sibling_row["content"] == "A storm-soaked rewrite."

    # The per-fragment branch engaged: the director fanned out more than the single
    # direct_scene call that combined mode would issue.
    director_calls = [c for c in captured if c["pass"] == "director"]
    assert len(director_calls) > 1

    # The user's direction reached every per-fragment director prompt.
    assert _DIRECTION in json.dumps(director_calls, default=str)
