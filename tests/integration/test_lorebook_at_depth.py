"""Import @ Depth lore and drive two turns to verify fresh tail rolls, constant
system lore and removal of inline comments. Teardown checks prefix stability.
"""

import re
from unittest import mock

import pytest

# The World Info export shape: entries as an object, `comment` as the
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
    world = await client.post_json("/api/worlds", json={"is_global": True, "name": "V20"})
    await client.post_checked(f"/api/worlds/{world['id']}/import", json=_ST_PAYLOAD)
    return world["id"]


async def _make_conversation(client) -> str:
    card = await client.post_json("/api/characters", json={"is_global": True, "name": "Aria", "description": "An elf ranger."})
    return await client.create("/api/conversations", json={"character_card_id": card["id"]})


async def _send(client, cid: str, content: str) -> None:
    resp = await client.post_checked(f"/api/conversations/{cid}/send", json={"content": content})
    _ = resp.text  # drain the buffered SSE stream so the turn fully completes


def _writer_calls(llm_mock) -> list[dict]:
    return [c for c in llm_mock.captured if c["pass"] == "writer"]


def _tail_text(call: dict) -> str:
    content = call["messages"][-1]["content"]
    if isinstance(content, list):  # multimodal
        return "\n".join(p["text"] for p in content if p.get("type") == "text")
    return content


@pytest.mark.parametrize("rolls", [(8, 7, 8, 1, 2, 3), (8, 7, 8, 8, 7, 8)], ids=["different-pools", "repeated-pool"])
async def test_at_depth_entry_rides_the_tail_with_fresh_dice_each_turn(client, llm_mock, monkeypatch, rolls):
    await _import_v20(client)
    cid = await _make_conversation(client)

    # Control fresh draws so a legitimate repeated pool cannot fail the test.
    dice = mock.Mock(side_effect=rolls)
    monkeypatch.setattr("backend.core.macros.random.randint", dice)

    for turn in ("I attack the ghoul.", "I press the advantage."):
        llm_mock.enqueue_writer("The blade lands.")
        await _send(client, cid, turn)

    writers = _writer_calls(llm_mock)
    assert len(writers) == 2

    pools = []
    for call, user_text in zip(writers, ("I attack the ghoul.", "I press the advantage."), strict=True):
        tail = _tail_text(call)
        system = call["messages"][0]["content"]

        # The depth block is the last thing the model reads (depth 0).
        assert tail.index(user_text) < tail.index("**Lorebook (Depth)**")
        assert tail.rstrip().endswith("</v20>")
        # Depth entries leave the prefix; plain constants stay in it.
        assert "<v20>" not in system
        assert "## Lorebook\n\nCanon: The moon is shattered." in system
        # `disable: true` on import -> never rendered anywhere.
        assert "Append a health bar." not in tail and "Append a health bar." not in system
        # Author comments are stripped at render, and {{user}} resolved.
        assert "{{//" not in tail and "notes for the human" not in tail
        assert "Diff checks for User." in tail

        pool = re.search(r"Pool: ([\d, ]+)\.", tail)
        assert pool, "the dice pool must be resolved to literal numbers"
        assert "{{roll" not in tail
        pools.append(pool.group(1))

    # Each turn consumes three fresh d10 draws, even when both pools match.
    assert dice.call_args_list == [mock.call(1, 10)] * 6
    assert pools == [", ".join(str(n) for n in rolls[start : start + 3]) for start in (0, 3)]


async def test_context_size_accounts_for_the_depth_block(client, llm_mock):
    world_id = await _import_v20(client)
    cid = await _make_conversation(client)

    with_world = await client.get_json(f"/api/conversations/{cid}/context-size")
    rows = {row["kind"]: row for row in with_world["breakdown"]}
    assert rows["depth_lore"]["chars"] > 0
    assert rows["constant_lore"]["chars"] > 0

    assert (await client.put(f"/api/worlds/{world_id}", json={"is_global": False})).status_code == 200
    without = await client.get_json(f"/api/conversations/{cid}/context-size")
    assert next(row for row in without["breakdown"] if row["kind"] == "depth_lore")["chars"] == 0
    assert without["total_chars"] < with_world["total_chars"]


async def test_the_layouts_lore_text_frames_constant_and_depth_lore(client, llm_mock):
    await _import_v20(client)
    await client.expect(
        "patch", "/api/prompt-blocks/default:constant_lore", json={"content": "## World of {{char}}\n{{constant_lore}}"}
    )
    await client.expect("patch", "/api/prompt-blocks/default:depth_lore", json={"content": "___\n\nRules:\n{{depth_lore}}"})
    await client.expect(
        "patch", "/api/prompt-blocks/default:depth_lore", json={"content": "___\n\nRules only"}, expected_status=409
    )
    cid = await _make_conversation(client)

    llm_mock.enqueue_writer("The blade lands.")
    await _send(client, cid, "I attack the ghoul.")
    call = _writer_calls(llm_mock)[-1]

    assert "## World of Aria\nCanon: The moon is shattered." in call["messages"][0]["content"]
    assert "___\n\nRules:\nV20 Rules: <v20>" in _tail_text(call)
    assert "Lorebook" not in call["messages"][0]["content"] + _tail_text(call)
