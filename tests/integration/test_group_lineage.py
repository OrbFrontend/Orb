"""Group families -- a fork of a group is a branch of it, not a second group.

``conversations.group_root_id`` is what makes the sidebar show one entry per
group instead of one per conversation. These tests pin the three things that
would quietly break that: every fork path joining the source's family, the
family staying flat however deep the forking goes, and a family surviving the
deletion of the conversation it started as.
"""

from __future__ import annotations

from backend.database import add_message, get_conversation, set_active_leaf


async def _card(client, name: str) -> str:
    response = await client.post("/api/characters", json={"name": name})
    assert response.status_code == 200
    return response.json()["id"]


async def _group(client, title: str = "Campfire") -> dict:
    aria, kael = await _card(client, "Aria"), await _card(client, "Kael")
    response = await client.post(
        "/api/conversations",
        json={
            "kind": "group",
            "title": title,
            "members": [{"character_card_id": aria}, {"character_card_id": kael}],
        },
    )
    assert response.status_code == 200
    return response.json()


async def _history(cid: str, turns: int = 3) -> None:
    parent = None
    for index in range(turns):
        parent, _ = await add_message(cid, "user" if index % 2 == 0 else "assistant", f"Line {index}", index, parent_id=parent)
    await set_active_leaf(cid, parent)


async def _root_ids(client) -> dict[str, str | None]:
    rows = (await client.get("/api/conversations")).json()
    return {row["id"]: row["group_root_id"] for row in rows}


# ── forks join the family ────────────────────────────────────────────────────


async def test_group_checkpoint_joins_the_family_instead_of_founding_one(client):
    conv = await _group(client)
    await _history(conv["id"])

    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()

    assert conv["group_root_id"] is None, "the original group is the root of its own family"
    assert checkpoint["group_root_id"] == conv["id"]
    assert checkpoint["id"] != conv["id"]


async def test_compression_fork_joins_the_family(client):
    conv = await _group(client)
    await _history(conv["id"], turns=4)

    response = await client.post(
        f"/api/conversations/{conv['id']}/compress",
        json={"summary": "So far.", "keep_count": 2},
    )
    assert response.status_code == 200
    forked = await get_conversation(response.json()["new_conversation_id"])

    assert forked is not None and forked["group_root_id"] == conv["id"]


async def test_a_family_stays_flat_however_deep_the_forking_goes(client):
    """A checkpoint of a checkpoint points at the root, not at its parent.

    Depth would force the sidebar to walk a chain to find the group; a flat
    family is a single grouping key, which is the whole point of the column.
    """
    conv = await _group(client)
    await _history(conv["id"])

    first = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()
    second = (await client.post(f"/api/conversations/{first['id']}/checkpoint", json={})).json()

    assert first["group_root_id"] == conv["id"]
    assert second["group_root_id"] == conv["id"], "not the checkpoint it was taken from"


async def test_solo_forks_carry_no_family(client):
    card = await _card(client, "Solo")
    conv = (await client.post("/api/conversations", json={"character_card_id": card})).json()
    await _history(conv["id"])

    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()

    assert checkpoint["kind"] == "solo"
    assert checkpoint["group_root_id"] is None


async def test_conversion_to_group_founds_a_family(client):
    card = await _card(client, "Ada")
    conv = (await client.post("/api/conversations", json={"character_card_id": card})).json()

    response = await client.post(f"/api/conversations/{conv['id']}/convert-to-group")
    assert response.status_code == 200
    converted = response.json()["conversation"]

    assert converted["kind"] == "group" and converted["group_root_id"] is None

    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()
    assert checkpoint["group_root_id"] == conv["id"]


# ── new conversation in an existing group ────────────────────────────────────


async def test_new_group_conversation_carries_the_cast_but_no_history(client):
    conv = await _group(client)
    await _history(conv["id"])

    fresh = (await client.post(f"/api/conversations/{conv['id']}/group-conversation")).json()

    assert fresh["kind"] == "group" and fresh["group_root_id"] == conv["id"]
    assert (await client.get(f"/api/conversations/{fresh['id']}/messages")).json() == []

    def roster(members):
        return [(m["speaker_key"], m["character_card_id"], m["display_name"]) for m in members]

    source_members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    fresh_members = (await client.get(f"/api/conversations/{fresh['id']}/members")).json()
    assert roster(fresh_members) == roster(source_members)
    # A new roster identity, not a shared one: members belong to a conversation.
    assert {m["id"] for m in fresh_members}.isdisjoint({m["id"] for m in source_members})


async def test_group_only_routes_reject_a_solo_conversation(client):
    card = await _card(client, "Solo")
    conv = (await client.post("/api/conversations", json={"character_card_id": card})).json()

    assert (await client.post(f"/api/conversations/{conv['id']}/group-conversation")).status_code == 409
    assert (await client.delete(f"/api/conversations/{conv['id']}/group")).status_code == 409


# ── deletion ─────────────────────────────────────────────────────────────────


async def test_deleting_the_root_promotes_the_oldest_survivor(client):
    """The family outlives its founding conversation.

    Without promotion the FK clears every fork's ``group_root_id`` and each one
    resurfaces as its own group -- the duplication this feature exists to stop,
    arriving by a different door.
    """
    conv = await _group(client)
    await _history(conv["id"])
    first = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()
    second = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={"title": "Second"})).json()

    assert (await client.delete(f"/api/conversations/{conv['id']}")).status_code == 200

    roots = await _root_ids(client)
    assert conv["id"] not in roots
    assert roots[first["id"]] is None, "the oldest survivor is the new root"
    assert roots[second["id"]] == first["id"]


async def test_deleting_a_fork_leaves_the_rest_of_the_family_alone(client):
    conv = await _group(client)
    await _history(conv["id"])
    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()

    assert (await client.delete(f"/api/conversations/{checkpoint['id']}")).status_code == 200

    roots = await _root_ids(client)
    assert checkpoint["id"] not in roots
    assert roots[conv["id"]] is None


async def test_deleting_the_group_takes_the_whole_family(client):
    conv = await _group(client)
    await _history(conv["id"])
    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()
    bystander = await _group(client, title="Elsewhere")

    response = await client.delete(f"/api/conversations/{conv['id']}/group")
    assert response.status_code == 200
    assert response.json()["deleted"] == 2

    remaining = await _root_ids(client)
    assert conv["id"] not in remaining and checkpoint["id"] not in remaining
    assert bystander["id"] in remaining


async def test_deleting_the_group_from_a_fork_resolves_the_root_first(client):
    """The sidebar passes whichever conversation is open, root or not."""
    conv = await _group(client)
    await _history(conv["id"])
    checkpoint = (await client.post(f"/api/conversations/{conv['id']}/checkpoint", json={})).json()

    response = await client.delete(f"/api/conversations/{checkpoint['id']}/group")
    assert response.status_code == 200
    assert response.json()["deleted"] == 2

    remaining = await _root_ids(client)
    assert conv["id"] not in remaining and checkpoint["id"] not in remaining
