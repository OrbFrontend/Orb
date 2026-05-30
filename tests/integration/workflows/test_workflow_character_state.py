"""Per-character workflow state: storage round-trip and cross-conversation
serialization.

Covers the DB helper contract (per-slot read/write/remove keyed by
workflow_id, graceful no-op on a missing/dangling card) and the property
that makes the dedicated character-keyed lock necessary: two conversations
that share one character card must serialize their per-character
read-modify-write even though their conversation ids -- and therefore their
``workflow_state_lock`` keys -- differ.
"""

from __future__ import annotations

import asyncio

from backend.database import (
    create_character_card,
    create_conversation,
    get_workflow_character_state,
    set_workflow_character_state,
)

from ._fixtures import make_workflow, register_for_test


async def test_round_trip_and_slot_isolation(client):
    await create_character_card({"id": "char1", "name": "C1"})

    assert await get_workflow_character_state("char1", "wf_a") is None

    await set_workflow_character_state("char1", "wf_a", {"mood": "calm"})
    assert await get_workflow_character_state("char1", "wf_a") == {"mood": "calm"}

    await set_workflow_character_state("char1", "wf_b", {"seen": 3})
    assert await get_workflow_character_state("char1", "wf_a") == {"mood": "calm"}
    assert await get_workflow_character_state("char1", "wf_b") == {"seen": 3}

    await set_workflow_character_state("char1", "wf_a", None)
    assert await get_workflow_character_state("char1", "wf_a") is None
    assert await get_workflow_character_state("char1", "wf_b") == {"seen": 3}

    # A present-but-empty slot is not the same as an absent one: {} and None are distinct inputs to the setter.
    await set_workflow_character_state("char1", "wf_b", {})
    assert await get_workflow_character_state("char1", "wf_b") == {}


async def test_missing_card_degrades_without_raising(client):
    assert await get_workflow_character_state("ghost", "wf") is None
    # Writing to a card that does not exist matches zero rows and is a no-op.
    await set_workflow_character_state("ghost", "wf", {"k": 1})
    assert await get_workflow_character_state("ghost", "wf") is None


async def test_two_conversations_one_character_no_lost_write(client):
    """Concurrent on-demand triggers on two conversations that share one
    character must not lose a per-character increment. Their conversation
    ids differ, so ``workflow_state_lock`` keys differ and cannot serialize
    them; only the character-keyed lock can.
    """
    await create_character_card({"id": "shared", "name": "Shared"})
    await create_conversation("conv_a", "A", "Shared", "", character_card_id="shared")
    await create_conversation("conv_b", "B", "Shared", "", character_card_id="shared")

    wid = "char_counter"

    async def hook(ctx, _body):
        state = await get_workflow_character_state(ctx.character_id, wid) or {}
        state["n"] = int(state.get("n", 0)) + 1
        await set_workflow_character_state(ctx.character_id, wid, state)
        return {}

    wf = make_workflow(wid, on_demand=hook)

    with register_for_test(wf):
        n = 20
        requests = [
            client.post(
                f"/api/conversations/{'conv_a' if i % 2 == 0 else 'conv_b'}/workflows/{wid}/trigger",
                json={},
            )
            for i in range(n)
        ]
        results = await asyncio.gather(*requests)

    assert all(r.status_code == 200 for r in results), [r.status_code for r in results]
    assert await get_workflow_character_state("shared", wid) == {"n": n}
