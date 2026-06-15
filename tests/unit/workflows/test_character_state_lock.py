"""Pins ``workflow_character_state_lock`` keying semantics in isolation.

Same ``(character_id, workflow_id)`` pair must serialize; different pairs
must not. The key is the character id rather than the conversation id
because one character card is shared across many conversations, so this is
the lock that prevents a lost write when two conversations of the same
character read-modify-write the per-character slot concurrently. Tested
directly against the lock primitive so a failure here narrows the search to
the lock itself.
"""

from __future__ import annotations

import asyncio

from backend.core.locks import workflow_character_state_lock


async def _hold(character_id: str, wid: str, gate: asyncio.Event, release: asyncio.Event) -> None:
    async with workflow_character_state_lock(character_id, wid):
        gate.set()
        await release.wait()


async def test_same_pair_serializes():
    first_in = asyncio.Event()
    first_can_exit = asyncio.Event()
    second_in = asyncio.Event()
    second_can_exit = asyncio.Event()

    first = asyncio.create_task(_hold("char", "w", first_in, first_can_exit))
    await first_in.wait()

    second = asyncio.create_task(_hold("char", "w", second_in, second_can_exit))
    await asyncio.sleep(0.05)
    assert not second_in.is_set(), "second acquirer entered while first held the lock"

    first_can_exit.set()
    await first
    await second_in.wait()
    second_can_exit.set()
    await second


async def test_different_character_ids_do_not_serialize():
    in_a = asyncio.Event()
    in_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    task_a = asyncio.create_task(_hold("char_a", "w", in_a, release_a))
    task_b = asyncio.create_task(_hold("char_b", "w", in_b, release_b))

    await asyncio.wait_for(asyncio.gather(in_a.wait(), in_b.wait()), timeout=1.0)

    release_a.set()
    release_b.set()
    await asyncio.gather(task_a, task_b)


async def test_different_workflow_ids_do_not_serialize():
    in_a = asyncio.Event()
    in_b = asyncio.Event()
    release_a = asyncio.Event()
    release_b = asyncio.Event()

    task_a = asyncio.create_task(_hold("char", "w1", in_a, release_a))
    task_b = asyncio.create_task(_hold("char", "w2", in_b, release_b))

    await asyncio.wait_for(asyncio.gather(in_a.wait(), in_b.wait()), timeout=1.0)

    release_a.set()
    release_b.set()
    await asyncio.gather(task_a, task_b)
