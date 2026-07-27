"""Phase 3: host resources, views, assets, branch activation, and card writes.

The lifecycle suite proves a package installs; the flow-runtime suite proves it
executes. This one covers the surfaces Phase 3 added *around* execution -- the
bounded read projections, the compiled-view route, the asset route, the shared
branch action, and the first-party tag write -- plus the two reference packages
that exist because those surfaces do.

Everything drives the real routes. A test that reached past them would not
notice a route that stopped checking a grant, which is most of what is being
asserted here.
"""

from __future__ import annotations

import backend.database as dbmod
from backend.core import normalize_tags
from backend.features.extensions import telemetry
from backend.features.extensions.ctx import build_ctx
from backend.features.extensions.resources import ResourceRequest, resolve_resource
from tests.extension_packages import (
    conversation_map_package,
    manifest,
    orbext,
    tag_librarian_package,
)

from .conftest import install

CLASSIFY_CALL = {
    "tool_calls": [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "extension_output", "arguments": {"tags": ["noir", "invented"]}},
        }
    ]
}
"""What the mock returns for Tag Librarian's classifier.

``invented`` is not in the vocabulary on purpose: the whole reason
``list.intersect`` is in the flow is that a model asked to pick from a list will
sometimes return something adjacent to it.
"""


async def _conversation(cid: str = "conv-host") -> str:
    await dbmod.create_conversation(cid, "ext", "Bot", "a scenario")
    return cid


async def _tree(cid: str) -> list[int]:
    """A three-message trunk with a sibling reply, so the tree has a branch."""
    root, _ = await dbmod.add_message(cid, "user", "hello", 0)
    first, _ = await dbmod.add_message(cid, "assistant", "first reply", 0, parent_id=root)
    second, _ = await dbmod.add_message(cid, "assistant", "second reply", 0, parent_id=root)
    await dbmod.set_active_leaf(cid, first)
    return [root, first, second]


async def _card(card_id: str = "card-1", **fields) -> str:
    await dbmod.create_character_card({"id": card_id, "name": "Mara", "description": "A tired detective.", **fields})
    return card_id


# ── conversation.tree resource ──────────────────────────────────────────────


async def test_the_tree_resource_returns_every_node_and_the_active_path(client):
    cid = await _conversation()
    root, first, second = await _tree(cid)
    await install(client, conversation_map_package())

    body = (await client.get(f"/api/extensions/conversation-map/resources/conversation.tree?conversation_id={cid}")).json()
    assert [node["id"] for node in body["nodes"]] == [root, first, second]
    assert body["active_path"] == [root, first]
    assert body["active_leaf_id"] == first
    # The inactive sibling is present, which is the whole point: the active-path
    # route cannot express it and a branch map cannot be drawn without it.
    assert second not in body["active_path"]


async def test_tree_previews_need_their_own_grant(client):
    cid = await _conversation()
    await _tree(cid)
    granted = [
        entry
        for entry in (
            await client.post("/api/extensions/inspect-file", files={"file": ("p.orbext", conversation_map_package())})
        ).json()["permissions"]
        if entry["capability"] != "conversation.tree.previews"
    ]
    await install(client, conversation_map_package(), permissions=[entry["value"] for entry in granted])

    body = (await client.get(f"/api/extensions/conversation-map/resources/conversation.tree?conversation_id={cid}")).json()
    assert all("preview" not in node for node in body["nodes"])


async def test_a_resource_is_403_without_its_grant(client):
    cid = await _conversation()
    await install(client, conversation_map_package(), permissions=[])
    response = await client.get(f"/api/extensions/conversation-map/resources/conversation.tree?conversation_id={cid}")
    assert response.status_code == 403


async def test_a_conversation_scoped_resource_fails_with_no_conversation(client):
    """It fails rather than falling back to whatever chat happens to be open.

    Falling back is how a library-scoped action would silently read a
    conversation the user never pointed it at.
    """
    await install(client, conversation_map_package())
    response = await client.get("/api/extensions/conversation-map/resources/conversation.tree")
    assert response.status_code == 400


async def test_an_unknown_resource_name_is_a_404(client):
    await install(client, conversation_map_package())
    assert (await client.get("/api/extensions/conversation-map/resources/settings")).status_code == 404


# ── library.cards resource ──────────────────────────────────────────────────


async def test_the_library_resource_projects_only_its_declared_shape(client):
    await _card("card-1")
    await install(client, tag_librarian_package())
    body = (await client.get("/api/extensions/tag-librarian/resources/library.cards")).json()
    card = body["cards"][0]
    assert set(card) == {"id", "name", "tags", "state"}
    # `description` is read through ctx.character during the action, under
    # context.character.read -- never here, under the enumeration grant.
    assert "description" not in card


async def test_the_library_resource_projects_only_this_extensions_slot(client):
    card_id = await _card("card-1")
    await dbmod.set_workflow_character_state(card_id, "tag-librarian", {"tagged": True})
    await dbmod.set_workflow_character_state(card_id, "someone-else", {"secret": "no"})
    await install(client, tag_librarian_package())
    body = (await client.get("/api/extensions/tag-librarian/resources/library.cards")).json()
    assert body["cards"][0]["state"] == {"tagged": True}


async def test_the_library_cursor_walk_returns_every_card_exactly_once(client, monkeypatch):
    monkeypatch.setattr("backend.features.extensions.resources.MAX_RESOURCE_PAGE_ITEMS", 2)
    for index in range(5):
        await _card(f"card-{index}")
    await install(client, tag_librarian_package())

    seen: list[str] = []
    cursor = None
    for _ in range(10):
        query = f"?cursor={cursor}" if cursor else ""
        body = (await client.get(f"/api/extensions/tag-librarian/resources/library.cards{query}")).json()
        seen.extend(card["id"] for card in body["cards"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert sorted(seen) == [f"card-{i}" for i in range(5)]
    assert len(seen) == len(set(seen))


async def test_the_library_walk_has_a_stable_insertion_boundary(client, monkeypatch):
    """New rows wait for the next sweep; deleting one cannot shift survivors."""
    monkeypatch.setattr("backend.features.extensions.resources.MAX_RESOURCE_PAGE_ITEMS", 2)
    original = [f"card-{index}" for index in range(5)]
    for card_id in original:
        await _card(card_id)
    await install(client, tag_librarian_package())

    first = (await client.get("/api/extensions/tag-librarian/resources/library.cards")).json()
    seen = [card["id"] for card in first["cards"]]
    # Delete the highest rowid before inserting, so SQLite is free to reuse
    # that rowid. The creation-time half of the snapshot boundary must still
    # keep the replacement out of this walk.
    await dbmod.delete_character_card("card-4")
    await _card("card-added-during-walk")

    cursor = first["next_cursor"]
    while cursor:
        page = (await client.get(f"/api/extensions/tag-librarian/resources/library.cards?cursor={cursor}")).json()
        seen.extend(card["id"] for card in page["cards"])
        cursor = page["next_cursor"]

    assert seen == ["card-0", "card-1", "card-2", "card-3"]
    assert "card-added-during-walk" not in seen
    assert len(seen) == len(set(seen))


async def test_library_pages_shrink_to_the_encoded_byte_budget(client, monkeypatch):
    monkeypatch.setattr("backend.features.extensions.resources.MAX_RESOURCE_PAGE_ITEMS", 10)
    monkeypatch.setattr("backend.features.extensions.resources.MAX_RESOURCE_BYTES", 700)
    for index in range(3):
        card_id = await _card(f"card-{index}")
        await dbmod.set_workflow_character_state(card_id, "tag-librarian", {"note": "x" * 300})
    await install(client, tag_librarian_package())

    seen: list[str] = []
    cursor = None
    page_sizes: list[int] = []
    while True:
        query = f"?cursor={cursor}" if cursor else ""
        response = await client.get(f"/api/extensions/tag-librarian/resources/library.cards{query}")
        assert response.status_code == 200, response.text
        body = response.json()
        page_sizes.append(len(body["cards"]))
        seen.extend(card["id"] for card in body["cards"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert seen == ["card-0", "card-1", "card-2"]
    assert page_sizes == [1, 1, 1]


async def test_lorebook_pages_shrink_to_the_encoded_byte_budget(client, monkeypatch):
    monkeypatch.setattr("backend.features.extensions.resources.MAX_RESOURCE_PAGE_ITEMS", 10)
    monkeypatch.setattr("backend.features.extensions.resources.MAX_RESOURCE_BYTES", 700)
    world = await dbmod.create_world({"name": "Archive"})
    card_id = await _card(world_id=world["id"])
    cid = "conv-lore-resource"
    await dbmod.create_conversation(cid, "Lore", "Mara", "", character_card_id=card_id)
    for index in range(3):
        await dbmod.create_lorebook_entry(
            world["id"],
            {"name": f"entry-{index}", "content": "x" * 300, "keywords": [f"key-{index}"]},
        )

    request = ResourceRequest(
        extension_id="lore-reader",
        resource="lorebook.entries",
        granted=frozenset({("lorebook.read", None)}),
        conversation_id=cid,
    )
    seen: list[int] = []
    page_sizes: list[int] = []
    while True:
        body = await resolve_resource(request)
        page_sizes.append(len(body["entries"]))
        seen.extend(entry["id"] for entry in body["entries"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
        request = ResourceRequest(
            extension_id=request.extension_id,
            resource=request.resource,
            granted=request.granted,
            conversation_id=cid,
            cursor=cursor,
        )

    assert len(seen) == 3
    assert len(seen) == len(set(seen))
    assert page_sizes == [1, 1, 1]


async def test_a_package_supplied_cursor_is_rejected(client):
    await _card()
    await install(client, tag_librarian_package())
    for forged in ("0", "eyJhZnRlciI6ICJjYXJkLTAifQ", "../../etc"):
        response = await client.get(f"/api/extensions/tag-librarian/resources/library.cards?cursor={forged}")
        assert response.status_code == 400, forged


async def test_a_cursor_cannot_be_replayed_against_another_resource(client, monkeypatch):
    monkeypatch.setattr("backend.features.extensions.resources.MAX_RESOURCE_PAGE_ITEMS", 1)
    await _card("card-0")
    await _card("card-1")
    await install(client, tag_librarian_package())
    cursor = (await client.get("/api/extensions/tag-librarian/resources/library.cards")).json()["next_cursor"]
    assert cursor
    response = await client.get(f"/api/extensions/tag-librarian/resources/direction.notes?cursor={cursor}")
    # Refused on the cursor, not resolved into some other resource's position.
    assert response.status_code in (400, 403)


# ── views and assets ────────────────────────────────────────────────────────


async def test_a_view_carries_its_compiled_tree_and_resolved_data(client):
    cid = await _conversation()
    await _tree(cid)
    await install(client, conversation_map_package())
    body = (await client.get(f"/api/extensions/conversation-map/views/map?conversation_id={cid}")).json()
    assert body["view"]["root"]["component"] == "card"
    assert body["data"]["tree"]["active_path"]
    assert body["errors"] == {}


async def test_a_view_reports_a_missing_grant_per_source_not_as_a_failure(client):
    """A partially granted package renders what it may, with the rest saying why."""
    cid = await _conversation()
    await _tree(cid)
    inspection = (
        await client.post("/api/extensions/inspect-file", files={"file": ("p.orbext", conversation_map_package())})
    ).json()
    approved = [entry["value"] for entry in inspection["permissions"] if entry["capability"] != "conversation.tree.read"]
    await install(client, conversation_map_package(), permissions=approved)
    response = await client.get(f"/api/extensions/conversation-map/views/map?conversation_id={cid}")
    # The view itself is blocked (its resource requirement is unmet), or it
    # renders with a per-source error. Either is a refusal the user can read;
    # what must not happen is an unguarded projection.
    assert response.status_code == 403 or response.json()["errors"]


async def test_a_view_reports_a_missing_state_source_grant(client):
    package = orbext(
        {
            "orb-extension.json": manifest(
                id="state-reader",
                requires={"components": ["text"]},
                permissions=[
                    {"capability": "state.read", "scope": "conversation"},
                    {"capability": "ui.contribute", "slot": "workspace"},
                ],
                views={"status": {"source": "ui/status.json"}},
                placements=[{"slot": "workspace", "view": "status"}],
            ),
            "ui/status.json": {
                "view_version": 1,
                "data": {"stored": {"kind": "state", "scope": "conversation"}},
                "root": {"component": "text", "value": "Status"},
            },
        }
    )
    await install(
        client,
        package,
        permissions=[{"capability": "ui.contribute", "slot": "workspace"}],
    )

    response = await client.get("/api/extensions/state-reader/views/status")
    assert response.status_code == 200
    assert "state.read" in response.json()["errors"]["stored"]
    assert response.json()["state"] == {}


async def test_revoking_sweep_bookkeeping_read_unpublishes_the_sweep_view(client):
    await _card()
    await install(client, tag_librarian_package())
    keep = [
        permission["value"]
        for permission in (await client.get("/api/extensions/tag-librarian")).json()["permissions"]
        if not (permission["capability"] == "state.read" and permission["parameters"].get("scope") == "character")
    ]
    await client.put("/api/extensions/tag-librarian/permissions", json={"permissions": keep})

    catalog = (await client.get("/api/extensions")).json()["extensions"]
    librarian = next(entry for entry in catalog if entry["id"] == "tag-librarian")
    assert librarian["placements"] == []
    assert (await client.get("/api/extensions/tag-librarian/views/workspace")).status_code == 403


async def test_an_undeclared_view_is_a_404(client):
    await install(client, conversation_map_package())
    assert (await client.get("/api/extensions/conversation-map/views/nope")).status_code == 404


async def test_an_asset_route_resolves_only_compiled_asset_keys(client):
    await install(client, conversation_map_package())
    for path in ("ui/map.json", "orb-extension.json", "../orb-extension.json", "flows/select-branch.json"):
        assert (await client.get(f"/api/extensions/conversation-map/assets/{path}")).status_code == 404, path


# ── branch activation ───────────────────────────────────────────────────────


async def test_branch_activation_matches_the_built_in_switch_route(client):
    cid = await _conversation()
    root, first, second = await _tree(cid)
    await install(client, conversation_map_package())

    response = await client.post(
        "/api/extensions/conversation-map/actions/select",
        json={"conversation_id": cid, "input": {"message_id": second}},
    )
    assert response.status_code == 200, response.text
    conv = await dbmod.get_conversation(cid)
    assert conv["active_leaf_id"] == second

    resources = {effect["resource"] for effect in response.json()["effects"]}
    assert resources == {"conversation.messages", "conversation.director", "conversation.direction_notes"}


async def test_selecting_a_foreign_message_fails_and_changes_nothing(client):
    cid = await _conversation()
    other = await _conversation("conv-host-other")
    _, first, _ = await _tree(cid)
    foreign, _ = await dbmod.add_message(other, "user", "elsewhere", 0)
    await install(client, conversation_map_package())

    response = await client.post(
        "/api/extensions/conversation-map/actions/select",
        json={"conversation_id": cid, "input": {"message_id": foreign}},
    )
    assert response.status_code == 400
    assert (await dbmod.get_conversation(cid))["active_leaf_id"] == first


async def test_revoking_branch_activation_blocks_the_action(client):
    cid = await _conversation()
    _, _, second = await _tree(cid)
    await install(client, conversation_map_package())
    keep = [
        entry["value"]
        for entry in (await client.get("/api/extensions/conversation-map")).json()["permissions"]
        if entry["capability"] != "conversation.branch.activate"
    ]
    await client.put("/api/extensions/conversation-map/permissions", json={"permissions": keep})

    response = await client.post(
        "/api/extensions/conversation-map/actions/select",
        json={"conversation_id": cid, "input": {"message_id": second}},
    )
    assert response.status_code == 403


# ── card.tags.set and action-input card resolution ──────────────────────────


async def _install_librarian(client, vocabulary=("noir", "detective")):
    await client.put("/api/settings", json={"enable_agent": False})
    await install(client, tag_librarian_package())
    await dbmod.set_workflow_config("tag-librarian", {"vocabulary": list(vocabulary)})


async def test_a_classify_action_writes_only_vocabulary_tags(client, db, llm_mock):
    card_id = await _card()
    await _install_librarian(client)
    llm_mock.enqueue_workflow(CLASSIFY_CALL)

    response = await client.post(
        "/api/extensions/tag-librarian/actions/classify",
        json={"input": {"card_id": card_id}},
    )
    assert response.status_code == 200, response.text
    card = await dbmod.get_character_card(card_id)
    # "invented" was in the model's output and is not in the vocabulary.
    assert card["tags"] == ["noir"]
    assert response.json()["effects"] == [{"resource": "character.card", "card_id": card_id}]


async def test_a_classify_action_scopes_character_state_to_the_named_card(client, db, llm_mock):
    """One package-supplied identifier moves both the read and the write target.

    That is intended -- a per-card record belongs on the card it describes --
    and it is why the resolution is gated by two grants rather than one.
    """
    card_id = await _card("card-target")
    await _card("card-other")
    await _install_librarian(client)
    llm_mock.enqueue_workflow(CLASSIFY_CALL)

    await client.post("/api/extensions/tag-librarian/actions/classify", json={"input": {"card_id": card_id}})
    assert await dbmod.get_workflow_character_state(card_id, "tag-librarian") == {"tagged": True}
    assert await dbmod.get_workflow_character_state("card-other", "tag-librarian") is None


async def test_naming_a_card_needs_both_grants(client):
    card_id = await _card()
    await client.put("/api/settings", json={"enable_agent": False})
    inspection = (
        await client.post("/api/extensions/inspect-file", files={"file": ("p.orbext", tag_librarian_package())})
    ).json()
    approved = [entry["value"] for entry in inspection["permissions"] if entry["capability"] != "library.cards.read"]
    await install(client, tag_librarian_package(), permissions=approved)

    response = await client.post(
        "/api/extensions/tag-librarian/actions/classify",
        json={"input": {"card_id": card_id}},
    )
    assert response.status_code == 403
    # The enumeration route is refused for the same missing grant.
    assert (await client.get("/api/extensions/tag-librarian/resources/library.cards")).status_code == 403


async def test_a_nonexistent_card_id_fails_rather_than_falling_back(client):
    cid = await _conversation()
    await _card("card-real")
    await _install_librarian(client)
    response = await client.post(
        "/api/extensions/tag-librarian/actions/classify",
        json={"conversation_id": cid, "input": {"card_id": "card-missing"}},
    )
    assert response.status_code == 404


async def test_card_id_is_refused_outside_a_slot(client):
    """The out-of-band field belongs to the host-supplied path only.

    Accepting it here would be the dual-grant check's bypass, spelled as a
    convenience.
    """
    card_id = await _card()
    await _install_librarian(client)
    response = await client.post(
        "/api/extensions/tag-librarian/actions/classify",
        json={"card_id": card_id, "input": {"card_id": card_id}},
    )
    assert response.status_code == 422


async def test_a_card_actions_slot_click_needs_no_enumeration_grant(client, db, llm_mock):
    """The identifier came from a user click, so the package never named a card.

    Reach is still exactly one user-chosen card per invocation, which is why the
    section 6 rule is preserved rather than excepted.
    """
    card_id = await _card()
    await client.put("/api/settings", json={"enable_agent": False})
    # No `card_id` in the input schema: this variant never *names* a card, so it
    # derives no enumeration grant. That is exactly the difference the slot
    # exists to express.
    await install(
        client,
        tag_librarian_package(
            permissions=[
                {"capability": "context.character.read"},
                {"capability": "model.call", "lane": "agent"},
                {"capability": "state.read", "scope": "config"},
                {"capability": "state.write", "scope": "config"},
                {"capability": "state.write", "scope": "character"},
                {"capability": "card.tags.write"},
                {"capability": "ui.contribute", "slot": "library.card_actions"},
            ],
            actions={"classify": {"flow": "flows/classify.json", "label": "Classify card"}},
            views={},
            commands=[{"id": "retag", "label": "Re-tag", "icon": "tag", "action": "classify"}],
            placements=[{"slot": "library.card_actions", "command": "retag"}],
        ),
    )
    await dbmod.set_workflow_config("tag-librarian", {"vocabulary": ["noir"]})
    llm_mock.enqueue_workflow(CLASSIFY_CALL)

    response = await client.post(
        "/api/extensions/tag-librarian/actions/classify",
        json={"slot": "library.card_actions", "card_id": card_id, "input": {}},
    )
    assert response.status_code == 200, response.text
    assert (await dbmod.get_character_card(card_id))["tags"] == ["noir"]

    keep = [
        permission["value"]
        for permission in (await client.get("/api/extensions/tag-librarian")).json()["permissions"]
        if not (permission["capability"] == "ui.contribute" and permission["parameters"].get("slot") == "library.card_actions")
    ]
    await client.put("/api/extensions/tag-librarian/permissions", json={"permissions": keep})
    catalog = (await client.get("/api/extensions")).json()["extensions"]
    librarian = next(entry for entry in catalog if entry["id"] == "tag-librarian")
    assert librarian["placements"] == []

    response = await client.post(
        "/api/extensions/tag-librarian/actions/classify",
        json={"slot": "library.card_actions", "card_id": card_id, "input": {}},
    )
    assert response.status_code == 403


async def test_a_slot_claim_is_checked_against_the_compiled_placements(client):
    """A package cannot take the relaxed path without the placement it names."""
    card_id = await _card()
    await _install_librarian(client)
    response = await client.post(
        "/api/extensions/tag-librarian/actions/classify",
        json={"slot": "library.card_actions", "card_id": card_id, "input": {}},
    )
    assert response.status_code == 403


async def test_revoking_the_tag_write_fails_the_next_classify(client, db, llm_mock):
    card_a = await _card("card-a")
    card_b = await _card("card-b")
    await _install_librarian(client)
    llm_mock.enqueue_workflow(CLASSIFY_CALL)
    await client.post("/api/extensions/tag-librarian/actions/classify", json={"input": {"card_id": card_a}})
    assert (await dbmod.get_character_card(card_a))["tags"] == ["noir"]

    keep = [
        entry["value"]
        for entry in (await client.get("/api/extensions/tag-librarian")).json()["permissions"]
        if entry["capability"] != "card.tags.write"
    ]
    await client.put("/api/extensions/tag-librarian/permissions", json={"permissions": keep})

    response = await client.post("/api/extensions/tag-librarian/actions/classify", json={"input": {"card_id": card_b}})
    assert response.status_code in (400, 403)
    # The earlier card keeps its tags: revocation stops the next write, it does
    # not revert committed ones.
    assert (await dbmod.get_character_card(card_a))["tags"] == ["noir"]
    assert (await dbmod.get_character_card(card_b))["tags"] == []


async def test_a_library_action_takes_no_conversation_stream_lock(client, db, llm_mock):
    """A sweep and a live turn must make progress concurrently.

    Branch activation *does* take that lock, and the two are easy to conflate as
    "actions that write outside their own namespace" -- so this asserts the
    difference rather than leaving it to the reader.
    """
    from backend.core import locks

    cid = await _conversation()
    card_id = await _card()
    await _install_librarian(client)
    llm_mock.enqueue_workflow(CLASSIFY_CALL)

    async with locks.conversation_stream_lock(cid):
        response = await client.post(
            "/api/extensions/tag-librarian/actions/classify",
            json={"conversation_id": cid, "input": {"card_id": card_id}},
        )
    assert response.status_code == 200, response.text


# ── shared tag normalization ────────────────────────────────────────────────


async def test_the_character_api_and_card_tags_set_normalize_identically(client, db, llm_mock):
    messy = ["  Noir ", "noir", "", "NOIR", "x" * 200, *[f"tag{i}" for i in range(40)]]
    expected = normalize_tags(messy)

    api_card = await _card("card-api")
    await client.put(f"/api/characters/{api_card}", json={"tags": messy})
    assert (await dbmod.get_character_card(api_card))["tags"] == expected

    flow_card = await _card("card-flow")
    await _install_librarian(client, vocabulary=messy)
    llm_mock.enqueue_workflow(
        {
            "tool_calls": [
                {"id": "c", "type": "function", "function": {"name": "extension_output", "arguments": {"tags": messy[:32]}}}
            ]
        }
    )
    await client.post("/api/extensions/tag-librarian/actions/classify", json={"input": {"card_id": flow_card}})
    written = (await dbmod.get_character_card(flow_card))["tags"]
    assert written == normalize_tags(written)
    assert set(written) <= set(expected)


async def test_import_is_left_unnormalized(client):
    """Exported PNGs keep author fidelity; normalization is a rule about writes."""
    await dbmod.create_character_card({"id": "card-import", "name": "Imported", "tags": ["Noir", "noir", " spaced "]})
    assert (await dbmod.get_character_card("card-import"))["tags"] == ["Noir", "noir", " spaced "]


def test_character_context_contains_a_bounded_current_tag_list():
    ctx = build_ctx(
        extension_id="tag-librarian",
        hook="action",
        granted=frozenset({("context.character.read", None)}),
        card={
            "id": "card-1",
            "name": "Mara",
            "tags": [" Noir ", "noir", "x" * 500, *[f"tag-{index}" for index in range(50)]],
        },
    )
    assert ctx["character"]["tags"] == normalize_tags([" Noir ", "noir", "x" * 500, *[f"tag-{index}" for index in range(50)]])


async def test_catalog_preserves_community_command_availability_predicates(client):
    await install(client, conversation_map_package())
    catalog = (await client.get("/api/extensions")).json()["extensions"]
    entry = next(item for item in catalog if item["id"] == "conversation-map")
    command = next(item for item in entry["commands"] if item["id"] == "open-map")
    assert command["when"] == {"exists": {"$ref": "host.active_conversation_id"}}


# ── telemetry ───────────────────────────────────────────────────────────────


async def test_telemetry_is_recorded_and_never_reaches_the_package(client, db, llm_mock):
    telemetry.reset()
    card_id = await _card()
    await _install_librarian(client)
    llm_mock.enqueue_workflow(CLASSIFY_CALL)
    await client.post("/api/extensions/tag-librarian/actions/classify", json={"input": {"card_id": card_id}})

    detail = (await client.get("/api/extensions/tag-librarian")).json()
    assert detail["telemetry"]["invocations"] == 1
    assert detail["telemetry"]["model_calls"] == 1
    assert detail["telemetry"]["last_outcome"] == "ok"

    # Nothing timing-shaped is projected into a flow's context.
    from backend.features.extensions.ctx import build_ctx

    ctx = build_ctx(extension_id="tag-librarian", hook="action", granted=frozenset())
    assert "telemetry" not in ctx and "duration_ms" not in ctx


# ── consent combination banner ──────────────────────────────────────────────


async def test_the_banner_appears_only_with_network_plus_a_data_read(client):
    from backend.features.extensions.catalog import (
        COMBINATION_BANNER,
        combination_warning,
    )

    assert combination_warning([{"capability": "context.history.read"}]) is None
    assert combination_warning([{"capability": "network.request", "origin": "https://a.invalid"}]) is None
    assert (
        combination_warning(
            [{"capability": "network.request", "origin": "https://a.invalid"}, {"capability": "context.persona.read"}]
        )
        == COMBINATION_BANNER
    )


async def test_the_detail_banner_follows_the_approved_set_not_the_request(client):
    inspection = (
        await client.post("/api/extensions/inspect-file", files={"file": ("p.orbext", tag_librarian_package())})
    ).json()
    # Tag Librarian asks for no network origin, so neither surface warns.
    assert inspection["combination_warning"] is None
    await install(client, tag_librarian_package())
    assert (await client.get("/api/extensions/tag-librarian")).json()["combination_warning"] is None
