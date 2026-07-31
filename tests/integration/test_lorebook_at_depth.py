"""
test_lorebook_at_depth.py — SillyTavern ``@ Depth`` lorebook entries, real stack.

The community's "RPG-lite" lorebooks (V20 stat checks and friends) are always-on
rule blocks that ship a pre-rolled dice pool: the frontend resolves N ×
``{{roll::1d10}}`` per turn so the model consumes real entropy instead of
inventing a number. That only works if the entry is re-rendered *outside* the
cached prefix, which is what ``at_depth`` buys — SillyTavern's ``position: 4``.

Drives ``POST /api/worlds/{id}/import`` with the shape those files actually use,
then two real ``POST /send`` turns, and asserts on the bytes each pass shipped:
the depth block rides the tail after the user message with fresh dice per turn,
the plain constant entry still rides the system prefix, and ``{{//}}`` author
comments never reach the model. The ``llm_mock`` teardown adds the KV check for
free — a depth block that leaked into the system message would change its bytes
turn-over-turn and fail the invariant.
"""

from __future__ import annotations

import re

# The SillyTavern World Info export shape: entries as an object, `comment` as the
# title, `order` as priority, `position: 4` = "@ Depth", `disable` = opt-out.
_ST_PAYLOAD = {
    "entries": {
        "0": {
            "uid": 0,
            "key": [],
            "comment": "V20 Rules",
            "content": "{{// notes for the human, not the model }}\n<v20>Diff checks for {{user}}.\nPool: {{roll::1d10}}, {{roll::1d10}}, {{roll::1d10}}.</v20>",
            "constant": True,
            "position": 4,
            "order": 100,
        },
        "1": {
            "uid": 1,
            "key": [],
            "comment": "Canon",
            "content": "The moon is shattered.",
            "constant": True,
            "position": 0,
            "order": 50,
        },
        "2": {
            "uid": 2,
            "key": [],
            "comment": "Tracker",
            "content": "Append a health bar.",
            "constant": True,
            "position": 4,
            "order": 90,
            "disable": True,
        },
    }
}


async def _import_v20(client) -> str:
    world = (await client.post("/api/worlds", json={"name": "V20"})).json()
    imp = await client.post(f"/api/worlds/{world['id']}/import", json=_ST_PAYLOAD)
    assert imp.status_code == 200
    return world["id"]


async def _make_conversation(client) -> str:
    card = await client.post("/api/characters", json={"name": "Aria", "description": "An elf ranger."})
    assert card.status_code == 200
    conv = await client.post("/api/conversations", json={"character_card_id": card.json()["id"]})
    assert conv.status_code == 200
    return conv.json()["id"]


async def _send(client, cid: str, content: str) -> None:
    resp = await client.post(f"/api/conversations/{cid}/send", json={"content": content})
    assert resp.status_code == 200
    _ = resp.text  # drain the buffered SSE stream so the turn fully completes


def _writer_calls(llm_mock) -> list[dict]:
    return [c for c in llm_mock.captured if c["pass"] == "writer"]


def _tail_text(call: dict) -> str:
    content = call["messages"][-1]["content"]
    if isinstance(content, list):  # multimodal
        return "\n".join(p["text"] for p in content if p.get("type") == "text")
    return content


async def test_at_depth_entry_rides_the_tail_with_fresh_dice_each_turn(client, llm_mock):
    await _import_v20(client)
    cid = await _make_conversation(client)

    for turn in ("I attack the ghoul.", "I press the advantage."):
        llm_mock.enqueue_writer("The blade lands.")
        await _send(client, cid, turn)

    writers = _writer_calls(llm_mock)
    assert len(writers) == 2

    pools = []
    for call, user_text in zip(writers, ("I attack the ghoul.", "I press the advantage."), strict=True):
        tail = _tail_text(call)
        system = call["messages"][0]["content"]

        # The depth block is the last thing the model reads (ST's depth 0).
        assert tail.index(user_text) < tail.index("**Lorebook (Depth)**")
        assert tail.rstrip().endswith("</v20>")
        # Depth entries leave the prefix; plain constants stay in it.
        assert "<v20>" not in system
        assert "## Lorebook\n\nCanon: The moon is shattered." in system
        # `disable: true` on import → never rendered anywhere.
        assert "Append a health bar." not in tail and "Append a health bar." not in system
        # Author comments are stripped at render, and {{user}} resolved.
        assert "{{//" not in tail and "notes for the human" not in tail
        assert "Diff checks for User." in tail

        pool = re.search(r"Pool: ([\d, ]+)\.", tail)
        assert pool, "the dice pool must be resolved to literal numbers"
        assert "{{roll" not in tail
        pools.append(pool.group(1))

    # Three independent d10 per turn, re-rolled every turn: a seeded (frozen)
    # resolution would repeat the pool verbatim. 1/1000 chance of a false failure.
    assert all(1 <= int(n) <= 10 for n in pools[0].split(", "))
    assert pools[0] != pools[1], f"dice froze across turns: {pools}"


async def test_context_size_accounts_for_the_depth_block(client, llm_mock):
    world_id = await _import_v20(client)
    cid = await _make_conversation(client)

    with_world = (await client.get(f"/api/conversations/{cid}/context-size")).json()
    assert with_world["breakdown"]["lorebook_depth"]["chars"] > 0
    assert with_world["breakdown"]["lorebook_constant"]["chars"] > 0

    assert (await client.put(f"/api/worlds/{world_id}", json={"enabled": False})).status_code == 200
    without = (await client.get(f"/api/conversations/{cid}/context-size")).json()
    assert without["breakdown"]["lorebook_depth"]["chars"] == 0
    assert without["total_chars"] < with_world["total_chars"]
