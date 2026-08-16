"""End-to-end coverage for Dynamic Worlds: proposal → review → World.

Two halves, matching the two halves of the feature.

**Pipeline** -- every main-pipeline entry point that finishes a reply gets one
forced ``propose_world_changes`` call for all enabled opted-in Worlds, judged on
the final post-editor prose, and split into one pending changeset per World at
the same persistence boundary as the reply itself.

**Lifecycle** -- what the review queue then does: a pending change is invisible
to everyone, accepting it makes it visible to every character sharing the World,
and the revision stamp is what makes exactly one of two concurrent accepts win.
"""

from __future__ import annotations

import asyncio

import backend.database as dbmod
from backend.pipeline import (
    handle_fork_edit,
    handle_magic_rewrite,
    handle_regenerate,
    handle_super_regenerate,
    handle_turn,
)

_ENTRY = {
    "name": "The Bridge",
    "content": "The stone bridge spans the gorge.",
    "keywords": ["bridge"],
}


def _propose(summary: str = "The bridge fell.", operations: list[dict] | None = None) -> list[dict]:
    """A ``propose_world_changes`` tool call in the OpenAI wire shape."""
    if operations is None:
        operations = [
            {
                "op": "create",
                "name": "Collapsed Bridge",
                "content": "The stone bridge has collapsed into the gorge.",
                "activation": "keywords",
                "keywords": ["bridge"],
                "rationale": "The bridge fell during the crossing.",
            }
        ]
    return [
        {
            "type": "function",
            "function": {
                "name": "propose_world_changes",
                "arguments": {"summary": summary, "operations": operations},
            },
        }
    ]


async def _drain(agen) -> list[dict]:
    return [ev async for ev in agen]


def _world_calls(llm_mock) -> list[dict]:
    """Every captured call that forced ``propose_world_changes``."""
    out = []
    for call in llm_mock.captured:
        choice = call.get("tool_choice")
        if isinstance(choice, dict) and choice.get("function", {}).get("name") == "propose_world_changes":
            out.append(call)
    return out


async def _world_with_character(client, *, dynamic: bool = True, name: str = "Gorge") -> tuple[str, str]:
    """A World (optionally Dynamic-enabled) linked to one character card."""
    world = (await client.post("/api/worlds", json={"name": name})).json()
    await client.post(f"/api/worlds/{world['id']}/entries", json=_ENTRY)
    if dynamic:
        await client.put(f"/api/worlds/{world['id']}/dynamic", json={"enabled": True})
    card = (await client.post("/api/characters", json={"name": f"{name} Guide"})).json()
    await client.put(f"/api/characters/{card['id']}", json={"world_id": world["id"]})
    return world["id"], card["id"]


async def _conversation(cid: str, card_id: str, name: str = "Guide") -> None:
    await dbmod.create_conversation(cid, "chat", name, "a scenario", character_card_id=card_id)


async def _pending(client, world_id: str) -> list[dict]:
    return (await client.get(f"/api/worlds/{world_id}/changesets", params={"status": "pending"})).json()


async def _effective_names(client, world_id: str) -> list[str]:
    rows = (await client.get(f"/api/worlds/{world_id}/entries", params={"view": "effective"})).json()
    return [r["name"] for r in rows]


# ── pipeline: the proposal pass ───────────────────────────────────────────────


async def test_a_completed_turn_stages_a_pending_proposal(client, db, llm_mock):
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-1", card_id)

    llm_mock.enqueue_writer("The bridge groans and gives way beneath you.")
    llm_mock.enqueue_world_change(_propose())

    events = await _drain(handle_turn("conv-dw-1", "I step onto the bridge"))

    proposed = [e for e in events if e.get("event") == "world_change_proposed"]
    assert len(proposed) == 1
    changeset = proposed[0]["data"]["changeset"]
    assert changeset["status"] == "pending"
    assert changeset["summary"] == "The bridge fell."
    assert [o["op"] for o in changeset["operations"]] == ["create"]

    # The event is ordered before `done` so one repaint finalises both.
    names = [e["event"] for e in events]
    assert names.index("world_change_proposed") < names.index("done")

    # Sourced to the exchange that produced it, with denormalised labels.
    assert changeset["source_assistant_message_id"] == proposed[0]["data"]["message_id"]
    assert changeset["source_conversation_id"] == "conv-dw-1"
    assert changeset["source_character_label"]


async def test_a_pending_proposal_is_not_lore_yet(client, llm_mock):
    """Invisible everywhere until reviewed -- to the projection, and to any other
    character sharing the World."""
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-2", card_id)
    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose())
    await _drain(handle_turn("conv-dw-2", "I cross"))

    assert await _effective_names(client, world_id) == ["The Bridge"]
    active = (await client.get("/api/lorebook-entries/active")).json()
    assert [e["name"] for e in active] == ["The Bridge"]


async def test_no_proposal_when_the_world_has_not_opted_in(client, llm_mock):
    world_id, card_id = await _world_with_character(client, dynamic=False)
    await _conversation("conv-dw-3", card_id)
    llm_mock.enqueue_writer("It falls.")

    events = await _drain(handle_turn("conv-dw-3", "I cross"))

    assert not [e for e in events if e.get("event") == "world_change_proposed"]
    assert await _pending(client, world_id) == []
    assert "world_change" not in [c[0] for c in llm_mock.calls]


async def test_no_proposal_when_no_world_has_opted_in(client, llm_mock):
    card = (await client.post("/api/characters", json={"name": "Loner"})).json()
    await _conversation("conv-dw-4", card["id"])
    llm_mock.enqueue_writer("Nothing here.")

    events = await _drain(handle_turn("conv-dw-4", "hello"))
    assert not [e for e in events if e.get("event") == "world_change_proposed"]


async def test_an_opted_in_world_proposes_without_a_character_linking_it(client, llm_mock):
    """The target set is what the prompt was played against, not what a card names.

    A World that is enabled is feeding this turn's lore, so the exchange is
    evidence about it whether or not the speaking character's card points at it.
    """
    world = (await client.post("/api/worlds", json={"name": "Shared"})).json()
    await client.post(f"/api/worlds/{world['id']}/entries", json=_ENTRY)
    await client.put(f"/api/worlds/{world['id']}/dynamic", json={"enabled": True})
    card = (await client.post("/api/characters", json={"name": "Loner"})).json()
    await _conversation("conv-dw-4b", card["id"])

    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose())
    await _drain(handle_turn("conv-dw-4b", "I cross"))

    assert [c["summary"] for c in await _pending(client, world["id"])] == ["The bridge fell."]


async def test_a_disabled_world_is_never_a_target(client, llm_mock):
    """It fed nothing into the prompt, so nothing in the reply is about it."""
    world_id, card_id = await _world_with_character(client)
    await client.put(f"/api/worlds/{world_id}", json={"enabled": False})
    await _conversation("conv-dw-4c", card_id)
    llm_mock.enqueue_writer("It falls.")

    await _drain(handle_turn("conv-dw-4c", "I cross"))

    assert await _pending(client, world_id) == []
    assert not _world_calls(llm_mock)


async def test_one_call_proposes_to_every_opted_in_world(client, llm_mock):
    """Several Worlds, one judgement -- split into one changeset each."""
    gorge_id, card_id = await _world_with_character(client, name="Gorge")
    guild = (await client.post("/api/worlds", json={"name": "Guild"})).json()
    await client.put(f"/api/worlds/{guild['id']}/dynamic", json={"enabled": True})
    await _conversation("conv-dw-4d", card_id)

    llm_mock.enqueue_writer("The bridge falls; the guild records the loss.")
    llm_mock.enqueue_world_change(
        _propose(
            operations=[
                {
                    "op": "create",
                    "target_world": "Gorge",
                    "name": "Collapsed Bridge",
                    "content": "The bridge is gone.",
                    "activation": "constant",
                    "rationale": "r",
                },
                {
                    "op": "create",
                    "target_world": "Guild",
                    "name": "Bridge Levy",
                    "content": "The guild is owed for the bridge.",
                    "activation": "constant",
                    "rationale": "r",
                },
            ]
        )
    )

    events = await _drain(handle_turn("conv-dw-4d", "I cross"))

    # One call, N changesets: the cost is per turn, not per lorebook.
    assert len(_world_calls(llm_mock)) == 1
    assert len([e for e in events if e.get("event") == "world_change_proposed"]) == 2
    (gorge_cs,) = await _pending(client, gorge_id)
    (guild_cs,) = await _pending(client, guild["id"])
    assert [o["name"] for o in gorge_cs["operations"]] == ["Collapsed Bridge"]
    assert [o["name"] for o in guild_cs["operations"]] == ["Bridge Levy"]
    # The World stamp is a routing detail of the call, not part of the changeset.
    assert "world_id" not in gorge_cs["operations"][0]
    # Each names its own World's revision to race against.
    assert gorge_cs["base_revision"] == 1 and guild_cs["base_revision"] == 0


async def test_the_catalog_names_every_world_in_play(client, llm_mock):
    _, card_id = await _world_with_character(client, name="Gorge")
    guild = (await client.post("/api/worlds", json={"name": "Guild"})).json()
    await client.put(f"/api/worlds/{guild['id']}/dynamic", json={"enabled": True})
    await _conversation("conv-dw-4e", card_id)

    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose(operations=[]))
    await _drain(handle_turn("conv-dw-4e", "I cross"))

    (call,) = _world_calls(llm_mock)
    request = call["messages"][-1]["content"]
    assert "## Gorge" in request and "## Guild" in request


async def test_no_proposal_when_the_agent_is_off(client, llm_mock):
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-5", card_id)
    await client.put("/api/settings", json={"enable_agent": False})
    llm_mock.enqueue_writer("It falls.")

    await _drain(handle_turn("conv-dw-5", "I cross"))
    assert await _pending(client, world_id) == []


async def test_no_proposal_when_the_reply_is_empty(client, llm_mock):
    """An empty draft persists no message, so there is nothing to anchor a
    changeset to and no evidence to derive one from."""
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-6", card_id)
    llm_mock.enqueue_writer("")

    await _drain(handle_turn("conv-dw-6", "I cross"))
    assert await _pending(client, world_id) == []


async def test_the_proposal_judges_the_post_editor_text(client, llm_mock):
    """The step must see the prose that will be persisted, not the writer's draft."""
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-7", card_id)
    await client.put(
        "/api/settings",
        json={"length_guard_enabled": True, "length_guard_max_words": 1},
    )

    llm_mock.enqueue_writer("The writer's first draft, which the editor will replace entirely.")
    llm_mock.enqueue_editor(
        {
            "tool_calls": [
                {
                    "type": "function",
                    "function": {
                        "name": "editor_rewrite",
                        "arguments": {"rewritten_text": "FINAL PROSE."},
                    },
                }
            ]
        }
    )
    llm_mock.enqueue_editor(None)
    llm_mock.enqueue_world_change(_propose())

    await _drain(handle_turn("conv-dw-7", "I cross"))

    world_calls = _world_calls(llm_mock)
    assert world_calls, "the proposal step never ran"
    replayed = [m["content"] for m in world_calls[-1]["messages"] if m["role"] == "assistant"]
    assert "FINAL PROSE." in replayed
    assert not any("first draft" in c for c in replayed)


async def test_a_steered_regenerate_judges_the_original_user_message(client, llm_mock):
    """Orb's OOC steering prompt directs the writer; it is not a world event."""
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-8", card_id)
    llm_mock.enqueue_writer("First reply.")
    await _drain(handle_turn("conv-dw-8", "I step onto the bridge"))
    target = [m for m in await dbmod.get_messages("conv-dw-8") if m["role"] == "assistant"][-1]

    llm_mock.enqueue_writer("Second reply.")
    llm_mock.enqueue_world_change(_propose())
    await _drain(handle_super_regenerate("conv-dw-8", target["id"]))

    request = _world_calls(llm_mock)[-1]["messages"][-1]["content"]
    assert "I step onto the bridge" in request
    assert "instruction to the writer" in request


async def test_a_failed_proposal_call_never_costs_the_reply(client, llm_mock):
    """Nothing is enqueued for the world_change pass, so the mock raises."""
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-9", card_id)
    llm_mock.enqueue_writer("The bridge holds.")

    events = await _drain(handle_turn("conv-dw-9", "I cross"))

    assert "error" not in [e["event"] for e in events]
    assert [m["content"] for m in await dbmod.get_messages("conv-dw-9") if m["role"] == "assistant"] == ["The bridge holds."]
    assert await _pending(client, world_id) == []


async def test_a_proposal_that_validates_to_nothing_stages_nothing(client, llm_mock):
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-10", card_id)
    llm_mock.enqueue_writer("The bridge holds.")
    # A create with no content says nothing durable: rejected by validation.
    llm_mock.enqueue_world_change(
        _propose(
            operations=[
                {
                    "op": "create",
                    "name": "X",
                    "content": "",
                    "activation": "constant",
                    "rationale": "r",
                }
            ]
        )
    )

    events = await _drain(handle_turn("conv-dw-10", "I cross"))
    assert not [e for e in events if e.get("event") == "world_change_proposed"]
    assert await _pending(client, world_id) == []


async def test_every_entry_point_proposes(client, llm_mock):
    """send, continue, fork-edit, regenerate, super-regenerate and magic rewrite."""
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-11", card_id)

    def _stage(summary: str) -> None:
        llm_mock.enqueue_writer(f"reply for {summary}")
        llm_mock.enqueue_world_change(
            _propose(
                summary,
                [
                    {
                        "op": "create",
                        "name": summary,
                        "content": "body",
                        "activation": "constant",
                        "rationale": "r",
                    }
                ],
            )
        )

    _stage("send")
    await _drain(handle_turn("conv-dw-11", "one"))
    user_msg = [m for m in await dbmod.get_messages("conv-dw-11") if m["role"] == "user"][-1]
    asst = [m for m in await dbmod.get_messages("conv-dw-11") if m["role"] == "assistant"][-1]

    _stage("fork")
    await _drain(handle_fork_edit("conv-dw-11", user_msg["id"], "one, edited"))
    _stage("regen")
    await _drain(handle_regenerate("conv-dw-11", asst["id"]))
    _stage("super")
    await _drain(handle_super_regenerate("conv-dw-11", asst["id"]))
    _stage("magic")
    await _drain(handle_magic_rewrite("conv-dw-11", asst["id"], "make it darker"))

    summaries = {c["summary"] for c in await _pending(client, world_id)}
    assert summaries == {"send", "fork", "regen", "super", "magic"}

    # /continue reuses handle_turn with the user row already persisted.
    await dbmod.add_message("conv-dw-11", "user", "two", 99, parent_id=asst["id"])
    _stage("continue")
    await _drain(handle_turn("conv-dw-11", "two", skip_user_persist=True))
    assert "continue" in {c["summary"] for c in await _pending(client, world_id)}


async def test_the_proposal_call_lands_in_the_inspector_audit(client, db, llm_mock):
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-12", card_id)
    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose())

    await _drain(handle_turn("conv-dw-12", "I cross"))

    asst = [m for m in await dbmod.get_messages("conv-dw-12") if m["role"] == "assistant"][-1]
    log = await dbmod.get_director_log_for_message(asst["id"])
    assert any(c.get("name") == "propose_world_changes" for c in (log or {}).get("tool_calls", []))


async def test_the_message_projection_carries_its_changeset(client, llm_mock):
    world_id, card_id = await _world_with_character(client)
    await _conversation("conv-dw-13", card_id)
    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose())
    await _drain(handle_turn("conv-dw-13", "I cross"))

    messages = (await client.get("/api/conversations/conv-dw-13/messages")).json()
    assistant = [m for m in messages if m["role"] == "assistant"][-1]
    assert [c["summary"] for c in assistant["world_changesets"]] == ["The bridge fell."]
    assert "world_changesets" not in [m for m in messages if m["role"] == "user"][-1]


# ── lifecycle: review, apply, undo, reset ─────────────────────────────────────


async def _staged_proposal(client, llm_mock, cid: str, *, operations: list[dict] | None = None) -> tuple[str, dict]:
    world_id, card_id = await _world_with_character(client, name=f"World-{cid}")
    await _conversation(cid, card_id)
    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose(operations=operations))
    await _drain(handle_turn(cid, "I cross"))
    (changeset,) = await _pending(client, world_id)
    return world_id, changeset


async def test_applying_makes_it_visible_to_every_character_sharing_the_world(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-20")
    other = (await client.post("/api/characters", json={"name": "Second"})).json()
    await client.put(f"/api/characters/{other['id']}", json={"world_id": world_id})
    await _conversation("conv-dw-20b", other["id"], name="Second")

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    assert resp.status_code == 200
    assert resp.json()["status"] == "applied"

    assert sorted(await _effective_names(client, world_id)) == [
        "Collapsed Bridge",
        "The Bridge",
    ]

    # And the other character's next turn actually sees it in its prompt.
    llm_mock.enqueue_writer("I know of the collapse.")
    await _drain(handle_turn("conv-dw-20b", "tell me about the bridge"))
    writer_call = [c for c in llm_mock.captured if c.get("tool_choice") in (None, "none")][-1]
    assert "collapsed into the gorge" in str(writer_call["messages"])


async def test_a_replacement_hides_its_target_in_the_prompt(client, llm_mock):
    world_id, card_id = await _world_with_character(client, name="Replace")
    await _conversation("conv-dw-21", card_id)
    entry = (await client.get(f"/api/worlds/{world_id}/entries")).json()[0]
    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(
        _propose(
            operations=[
                {
                    "op": "replace",
                    "target_entry_id": entry["id"],
                    "name": "The Bridge",
                    "content": "Only splintered pilings remain.",
                    "activation": "keywords",
                    "keywords": ["bridge"],
                    "rationale": "it collapsed",
                }
            ]
        )
    )
    await _drain(handle_turn("conv-dw-21", "I cross"))
    (changeset,) = await _pending(client, world_id)
    await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})

    rows = (await client.get(f"/api/worlds/{world_id}/entries", params={"view": "effective"})).json()
    assert [r["content"] for r in rows] == ["Only splintered pilings remain."]
    # The authored row itself is untouched and still there, just hidden.
    authored = (await client.get(f"/api/worlds/{world_id}/entries", params={"view": "authored"})).json()
    assert [r["content"] for r in authored] == [_ENTRY["content"]]


async def test_rejecting_changes_nothing(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-22")
    before = (await client.get(f"/api/worlds/{world_id}")).json() if False else None  # noqa: F841 — see revision check below
    revision = (await client.get(f"/api/worlds/{world_id}/entries")).json()

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/reject")
    assert resp.status_code == 200 and resp.json()["status"] == "rejected"
    assert (await client.get(f"/api/worlds/{world_id}/entries")).json() == revision
    assert await _pending(client, world_id) == []


async def test_a_decided_changeset_cannot_be_decided_twice(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-23")
    await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/reject")
    again = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    assert again.status_code == 409


async def test_apply_and_reject_cannot_both_win(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-23-race")

    apply, reject = await asyncio.gather(
        client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={}),
        client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/reject"),
    )

    assert sorted((apply.status_code, reject.status_code)) == [200, 409]
    current = (await client.get(f"/api/worlds/{world_id}/changesets")).json()[0]
    if current["status"] == "applied":
        assert "Collapsed Bridge" in await _effective_names(client, world_id)
    else:
        assert current["status"] == "rejected"
        assert "Collapsed Bridge" not in await _effective_names(client, world_id)


async def test_editing_a_proposal_before_applying_commits_what_was_reviewed(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-24")
    edited = [
        {
            "op": "create",
            "name": "Rewritten By Hand",
            "content": "The user's own wording.",
            "activation": "constant",
            "keywords": [],
            "rationale": "r",
        }
    ]
    resp = await client.post(
        f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply",
        json={"summary": "hand-edited", "operations": edited},
    )
    assert resp.status_code == 200
    assert "Rewritten By Hand" in await _effective_names(client, world_id)
    assert "Collapsed Bridge" not in await _effective_names(client, world_id)


async def test_authored_crud_makes_an_older_proposal_stale(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-25")
    await client.post(
        f"/api/worlds/{world_id}/entries",
        json={"name": "Late addition", "content": "x"},
    )

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    assert resp.status_code == 409
    assert "Re-evaluate" in resp.json()["detail"]
    assert "Collapsed Bridge" not in await _effective_names(client, world_id)
    assert [c["status"] for c in await _pending(client, world_id)] == ["stale"]


async def test_toggling_a_world_does_not_invalidate_a_proposal(client, llm_mock):
    """The character-switch flow toggles `enabled`; that must not cost a proposal."""
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-26")
    await client.put(f"/api/worlds/{world_id}", json={"enabled": False})
    await client.put(f"/api/worlds/{world_id}", json={"enabled": True, "name": "Renamed"})
    await client.put(f"/api/worlds/{world_id}/dynamic", json={"enabled": False})

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    assert resp.status_code == 200


async def test_a_bulk_import_bumps_the_revision_exactly_once(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-27")
    before = (await client.get("/api/worlds")).json()
    revision = next(w["content_revision"] for w in before if w["id"] == world_id)

    await client.post(
        f"/api/worlds/{world_id}/import",
        json={"entries": [{"name": f"E{i}", "content": "x", "keys": ["k"]} for i in range(5)]},
    )

    after = (await client.get("/api/worlds")).json()
    assert next(w["content_revision"] for w in after if w["id"] == world_id) == revision + 1


async def test_exactly_one_of_two_concurrent_accepts_wins(client, llm_mock):
    world_id, card_id = await _world_with_character(client, name="Race")
    await _conversation("conv-dw-28", card_id)
    for i in range(2):
        llm_mock.enqueue_writer(f"reply {i}")
        llm_mock.enqueue_world_change(
            _propose(
                f"proposal {i}",
                [
                    {
                        "op": "create",
                        "name": f"Fact {i}",
                        "content": "body",
                        "activation": "constant",
                        "rationale": "r",
                    }
                ],
            )
        )
        await _drain(handle_turn("conv-dw-28", f"turn {i}"))

    pending = await _pending(client, world_id)
    assert len(pending) == 2
    # Both were proposed against the same World, so both hold the same base.
    assert len({c["base_revision"] for c in pending}) == 1

    results = await asyncio.gather(
        *(client.post(f"/api/worlds/{world_id}/changesets/{c['id']}/apply", json={}) for c in pending)
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409]
    assert len([n for n in await _effective_names(client, world_id) if n.startswith("Fact")]) == 1


async def test_undo_restores_the_previous_projection(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-29")
    applied = (await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})).json()
    assert "Collapsed Bridge" in await _effective_names(client, world_id)

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{applied['id']}/undo")
    assert resp.status_code == 200
    assert await _effective_names(client, world_id) == ["The Bridge"]

    history = (await client.get(f"/api/worlds/{world_id}/changesets", params={"status": "history"})).json()
    statuses = {c["id"]: c["status"] for c in history}
    assert statuses[applied["id"]] == "reverted"
    assert resp.json()["origin"] == "undo"


async def test_undo_refuses_when_the_entry_moved_on(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-30")
    applied = (await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})).json()
    created = applied["after_entries"][0]["id"]
    await client.put(
        f"/api/worlds/{world_id}/entries/{created}",
        json={"content": "hand-edited since"},
    )

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{applied['id']}/undo")
    assert resp.status_code == 409
    assert "Collapsed Bridge" in await _effective_names(client, world_id)
    changesets = (await client.get(f"/api/worlds/{world_id}/changesets")).json()
    assert not [c for c in changesets if c["origin"] == "undo"]


async def test_undo_refuses_after_a_non_content_overlay_edit(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-30-fields")
    applied = (await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})).json()
    created = applied["after_entries"][0]

    edited = (
        await client.put(
            f"/api/worlds/{world_id}/entries/{created['id']}",
            json={"at_depth": True},
        )
    ).json()
    assert edited["entry_revision"] == created["entry_revision"] + 1

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{applied['id']}/undo")
    assert resp.status_code == 409
    changesets = (await client.get(f"/api/worlds/{world_id}/changesets")).json()
    assert not [c for c in changesets if c["origin"] == "undo"]


async def _applied_overlay_on(client, llm_mock, cid: str, op: dict) -> tuple[str, int, dict]:
    """Apply one operation aimed at the World's single authored entry.

    Returns ``(world_id, authored_entry_id, applied_changeset)``.
    """
    world_id, card_id = await _world_with_character(client, name=f"World-{cid}")
    await _conversation(cid, card_id)
    authored = (await client.get(f"/api/worlds/{world_id}/entries")).json()[0]
    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose(operations=[{**op, "target_entry_id": authored["id"]}]))
    await _drain(handle_turn(cid, "I cross"))
    (changeset,) = await _pending(client, world_id)
    applied = (await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})).json()
    assert applied["status"] == "applied"
    return world_id, authored["id"], applied


_REPLACE_OP = {
    "op": "replace",
    "name": "The Bridge",
    "content": "Only splintered pilings remain.",
    "activation": "keywords",
    "keywords": ["bridge"],
    "rationale": "it collapsed",
}


async def test_deleting_a_superseded_authored_entry_keeps_the_accepted_replacement(client, llm_mock):
    """Tidying away the entry a replacement supersedes must not erase the replacement.

    Accepting a ``replace`` makes the authored row redundant, so deleting it is
    the natural next step -- and it used to cascade the overlay away with it,
    silently discarding lore the user had reviewed and accepted.
    """
    world_id, authored_id, _ = await _applied_overlay_on(client, llm_mock, "conv-dw-cascade-1", _REPLACE_OP)
    assert await _effective_names(client, world_id) == ["The Bridge"]

    resp = await client.delete(f"/api/worlds/{world_id}/entries/{authored_id}")
    assert resp.status_code == 200

    rows = (await client.get(f"/api/worlds/{world_id}/entries", params={"view": "effective"})).json()
    assert [(r["name"], r["content"]) for r in rows] == [("The Bridge", "Only splintered pilings remain.")]
    # The replacement now stands on its own: it hides nothing, so it reads as an add.
    assert rows[0]["entry_layer"] == "dynamic"
    assert rows[0]["supersedes_entry_id"] is None


async def test_undo_survives_its_replacements_authored_target_being_deleted(client, llm_mock):
    """A lost pointer is the user's own delete, not a later edit the undo would clobber."""
    world_id, authored_id, applied = await _applied_overlay_on(client, llm_mock, "conv-dw-cascade-2", _REPLACE_OP)
    await client.delete(f"/api/worlds/{world_id}/entries/{authored_id}")

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{applied['id']}/undo")
    assert resp.status_code == 200, resp.text
    # Undo retires what the changeset created; the authored row the user deleted
    # is not resurrected, so the World is simply empty.
    assert await _effective_names(client, world_id) == []
    history = {c["id"]: c["status"] for c in (await client.get(f"/api/worlds/{world_id}/changesets")).json()}
    assert history[applied["id"]] == "reverted"


async def test_undo_still_refuses_when_the_overlay_row_itself_was_edited(client, llm_mock):
    """The carve-out is the lost pointer alone -- every other field still guards."""
    world_id, authored_id, applied = await _applied_overlay_on(client, llm_mock, "conv-dw-cascade-3", _REPLACE_OP)
    created = applied["after_entries"][0]["id"]
    await client.delete(f"/api/worlds/{world_id}/entries/{authored_id}")
    await client.put(f"/api/worlds/{world_id}/entries/{created}", json={"content": "hand-edited since"})

    resp = await client.post(f"/api/worlds/{world_id}/changesets/{applied['id']}/undo")
    assert resp.status_code == 409
    assert await _effective_names(client, world_id) == ["The Bridge"]


async def test_deleting_a_suppressed_authored_entry_leaves_an_inert_marker(client, llm_mock):
    world_id, authored_id, _ = await _applied_overlay_on(
        client,
        llm_mock,
        "conv-dw-cascade-4",
        {"op": "suppress", "rationale": "it is gone"},
    )
    assert await _effective_names(client, world_id) == []

    await client.delete(f"/api/worlds/{world_id}/entries/{authored_id}")

    rows = (await client.get(f"/api/worlds/{world_id}/entries")).json()
    assert [(r["overlay_action"], r["supersedes_entry_id"]) for r in rows] == [("suppress", None)]
    assert await _effective_names(client, world_id) == []


# ── history: a deletion, by whichever hand ────────────────────────────────────


async def _history(client, world_id: str) -> list[dict]:
    return (await client.get(f"/api/worlds/{world_id}/changesets", params={"status": "history"})).json()


async def test_deleting_an_authored_entry_by_hand_lands_in_history(client):
    """A hand delete is the one drawer mutation that would otherwise vanish."""
    world = (await client.post("/api/worlds", json={"name": "Recorded"})).json()
    entry = (await client.post(f"/api/worlds/{world['id']}/entries", json=_ENTRY)).json()
    await client.put(f"/api/worlds/{world['id']}/dynamic", json={"enabled": True})

    assert (await client.delete(f"/api/worlds/{world['id']}/entries/{entry['id']}")).status_code == 200

    (record,) = await _history(client, world["id"])
    assert (record["status"], record["origin"]) == ("applied", "manual")
    assert record["summary"] == 'Deleted entry "The Bridge"'
    # The record has to still read correctly with the row it names gone, so it
    # carries the wording as well as the id.
    (op,) = record["operations"]
    assert (op["op"], op["target_entry_id"]) == ("delete", entry["id"])
    assert op["target_content"] == _ENTRY["content"]
    assert record["before_entries"][0]["name"] == "The Bridge"
    assert record["after_entries"] == [None]
    # It happened; it is not something the user still owes a decision on.
    assert await _pending(client, world["id"]) == []


async def test_deleting_an_agent_managed_entry_by_hand_says_whose_lore_it_was(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-del-1")
    applied = (await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})).json()
    created = applied["after_entries"][0]["id"]

    await client.delete(f"/api/worlds/{world_id}/entries/{created}")

    summaries = [c["summary"] for c in await _history(client, world_id) if c["origin"] == "manual"]
    assert summaries == ['Deleted Agent-managed entry "Collapsed Bridge"']
    assert await _effective_names(client, world_id) == ["The Bridge"]


async def test_an_agent_retraction_lands_in_the_same_history(client, llm_mock):
    """Both hands' removals read off one list — that is what makes it an account."""
    world_id, _, applied = await _applied_overlay_on(
        client,
        llm_mock,
        "conv-dw-del-2",
        {"op": "suppress", "rationale": "the bridge is gone"},
    )
    assert await _effective_names(client, world_id) == []

    (record,) = await _history(client, world_id)
    assert (record["id"], record["origin"], record["status"]) == (applied["id"], "agent", "applied")
    assert [op["op"] for op in record["operations"]] == ["suppress"]


async def test_a_recorded_deletion_cannot_be_undone(client):
    """The row is gone: there is nothing a compensating operation could restore."""
    world = (await client.post("/api/worlds", json={"name": "No Take-backs"})).json()
    entry = (await client.post(f"/api/worlds/{world['id']}/entries", json=_ENTRY)).json()
    await client.put(f"/api/worlds/{world['id']}/dynamic", json={"enabled": True})
    await client.delete(f"/api/worlds/{world['id']}/entries/{entry['id']}")
    (record,) = await _history(client, world["id"])

    resp = await client.post(f"/api/worlds/{world['id']}/changesets/{record['id']}/undo")
    assert resp.status_code == 409
    assert [c["id"] for c in await _history(client, world["id"])] == [record["id"]]


async def test_a_deletion_makes_an_older_proposal_stale_and_is_recorded_once(client, llm_mock):
    """One user action, one revision bump — the record must not cost a second."""
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-del-3")
    authored = [e for e in (await client.get(f"/api/worlds/{world_id}/entries")).json() if e["name"] == "The Bridge"][0]

    await client.delete(f"/api/worlds/{world_id}/entries/{authored['id']}")

    (record,) = [c for c in await _history(client, world_id) if c["origin"] == "manual"]
    assert record["applied_revision"] == record["base_revision"] + 1
    # The delete moved the World off exactly the revision the proposal named,
    # which is what the older proposal then loses its race against.
    assert record["base_revision"] == changeset["base_revision"]
    resp = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    assert resp.status_code == 409
    assert [c["status"] for c in await _pending(client, world_id)] == ["stale"]


async def test_a_world_that_never_opted_in_records_nothing(client):
    """A plain lorebook has no history for a deletion to join."""
    world = (await client.post("/api/worlds", json={"name": "Plain"})).json()
    entry = (await client.post(f"/api/worlds/{world['id']}/entries", json=_ENTRY)).json()

    assert (await client.delete(f"/api/worlds/{world['id']}/entries/{entry['id']}")).status_code == 200
    assert (await client.get(f"/api/worlds/{world['id']}/changesets")).json() == []
    assert (await client.delete(f"/api/worlds/{world['id']}/entries/{entry['id']}")).status_code == 404


async def test_reset_restores_the_authored_world_and_is_itself_undoable(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-31")
    await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    assert len(await _effective_names(client, world_id)) == 2

    resp = await client.post(f"/api/worlds/{world_id}/reset")
    assert resp.status_code == 200 and resp.json()["reset"] is True
    assert await _effective_names(client, world_id) == ["The Bridge"]

    reset_changeset = resp.json()["changeset"]
    undo = await client.post(f"/api/worlds/{world_id}/changesets/{reset_changeset['id']}/undo")
    assert undo.status_code == 200
    assert sorted(await _effective_names(client, world_id)) == [
        "Collapsed Bridge",
        "The Bridge",
    ]


async def test_reset_on_a_world_with_no_overlay_is_a_no_op(client):
    world = (await client.post("/api/worlds", json={"name": "Untouched"})).json()
    resp = await client.post(f"/api/worlds/{world['id']}/reset")
    assert resp.status_code == 200 and resp.json()["reset"] is False


async def test_editing_a_source_message_makes_the_proposal_stale(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-32")
    await client.post(
        f"/api/conversations/conv-dw-32/messages/{changeset['source_assistant_message_id']}/edit",
        json={"content": "Actually the bridge held.", "regenerate": False},
    )
    assert [c["status"] for c in await _pending(client, world_id)] == ["stale"]
    apply = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    assert apply.status_code == 409
    assert "Re-evaluate" in apply.json()["detail"]
    assert "Collapsed Bridge" not in await _effective_names(client, world_id)


async def test_deleting_the_source_keeps_applied_history_but_stales_the_pending(client, llm_mock):
    world_id, card_id = await _world_with_character(client, name="Deleted")
    await _conversation("conv-dw-33", card_id)
    for i in range(2):
        llm_mock.enqueue_writer(f"reply {i}")
        llm_mock.enqueue_world_change(
            _propose(
                f"proposal {i}",
                [
                    {
                        "op": "create",
                        "name": f"Fact {i}",
                        "content": "b",
                        "activation": "constant",
                        "rationale": "r",
                    }
                ],
            )
        )
        await _drain(handle_turn("conv-dw-33", f"turn {i}"))
    first, second = sorted(await _pending(client, world_id), key=lambda c: c["id"])
    await client.post(f"/api/worlds/{world_id}/changesets/{first['id']}/apply", json={})

    root = (await dbmod.get_messages("conv-dw-33"))[0]
    await client.delete(f"/api/conversations/conv-dw-33/messages/{root['id']}")

    history = (await client.get(f"/api/worlds/{world_id}/changesets", params={"status": "history"})).json()
    kept = next(c for c in history if c["id"] == first["id"])
    assert kept["status"] == "applied"
    assert kept["source_assistant_message_id"] is None
    assert kept["source_character_label"]  # the denormalised label survives
    assert "Fact 0" in await _effective_names(client, world_id)
    assert [c["status"] for c in await _pending(client, world_id) if c["id"] == second["id"]] == ["stale"]


async def test_re_evaluation_derives_a_fresh_proposal_from_the_current_world(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-34")
    await client.post(
        f"/api/worlds/{world_id}/entries",
        json={"name": "Late addition", "content": "authored since"},
    )
    assert (await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})).status_code == 409

    llm_mock.enqueue_world_change(
        _propose(
            "re-derived",
            [
                {
                    "op": "create",
                    "name": "Second Look",
                    "content": "b",
                    "activation": "constant",
                    "rationale": "r",
                }
            ],
        )
    )
    resp = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/re-evaluate")
    assert resp.status_code == 200
    replacement = resp.json()["changeset"]
    assert replacement["summary"] == "re-derived"
    assert replacement["supersedes_changeset_id"] == changeset["id"]
    all_changesets = (await client.get(f"/api/worlds/{world_id}/changesets")).json()
    original = next(c for c in all_changesets if c["id"] == changeset["id"])
    assert original["status"] == "superseded"
    assert [c["id"] for c in await _pending(client, world_id)] == [replacement["id"]]

    # A resolved original cannot be re-evaluated again to create sibling
    # replacements or keep charging the model from another tab.
    again = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/re-evaluate")
    assert again.status_code == 409
    # Based on the world as it now stands, so it applies cleanly.
    assert (await client.post(f"/api/worlds/{world_id}/changesets/{replacement['id']}/apply", json={})).status_code == 200

    # The step saw the entry that was added after the original proposal.
    assert "Late addition" in _world_calls(llm_mock)[-1]["messages"][-1]["content"]


async def test_re_evaluation_with_no_operations_still_retires_the_original(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-34-empty")
    await client.post(
        f"/api/worlds/{world_id}/entries",
        json={"name": "Late addition", "content": "authored since"},
    )
    assert (await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})).status_code == 409

    llm_mock.enqueue_world_change(_propose("nothing left", []))
    response = await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/re-evaluate")

    assert response.status_code == 200
    assert response.json()["changeset"] is None
    assert await _pending(client, world_id) == []
    history = (await client.get(f"/api/worlds/{world_id}/changesets", params={"status": "history"})).json()
    assert next(c for c in history if c["id"] == changeset["id"])["status"] == "superseded"


# ── export ────────────────────────────────────────────────────────────────────


async def test_effective_view_matches_prompt_when_a_replacement_is_disabled(
    client,
):
    world = (await client.post("/api/worlds", json={"name": "Projection"})).json()
    authored = (
        await client.post(
            f"/api/worlds/{world['id']}/entries",
            json={
                "name": "Bridge",
                "content": "The bridge stands.",
                "constant": True,
            },
        )
    ).json()
    await dbmod.create_lorebook_entry(
        world["id"],
        {
            "name": "Bridge",
            "content": "The bridge collapsed.",
            "constant": True,
            "enabled": False,
            "entry_layer": "dynamic",
            "overlay_action": "replace",
            "supersedes_entry_id": authored["id"],
        },
    )

    effective = (await client.get(f"/api/worlds/{world['id']}/entries", params={"view": "effective"})).json()
    active = (await client.get("/api/lorebook-entries/active")).json()

    assert [(e["id"], e["content"]) for e in effective] == [(authored["id"], "The bridge stands.")]
    assert [(e["id"], e["content"]) for e in active] == [(authored["id"], "The bridge stands.")]


async def test_export_defaults_to_authored_and_effective_is_opt_in(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-35")
    await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})

    default = (await client.get(f"/api/worlds/{world_id}/export")).json()
    assert [e["name"] for e in default["entries"]] == ["The Bridge"]

    effective = (await client.get(f"/api/worlds/{world_id}/export", params={"view": "effective"})).json()
    assert sorted(e["name"] for e in effective["entries"]) == [
        "Collapsed Bridge",
        "The Bridge",
    ]


async def test_card_export_embeds_the_authored_book_by_default(client, llm_mock):
    """A card shared with someone else carries the lore its author wrote, not
    whatever this playthrough's Agent proposed and its owner accepted."""
    world_id, card_id = await _world_with_character(client, name="Shared")
    await _conversation("conv-dw-37", card_id)
    llm_mock.enqueue_writer("It falls.")
    llm_mock.enqueue_world_change(_propose())
    await _drain(handle_turn("conv-dw-37", "I cross"))
    (changeset,) = await _pending(client, world_id)
    await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})

    def _names(png_bytes: bytes) -> list[str]:
        import base64
        import io
        import json

        from PIL import Image

        # Read the tEXt chunk through Pillow rather than scanning the bytes: a
        # regex for the base64 alphabet runs straight past the payload into the
        # chunk's CRC whenever those bytes happen to be alphabet characters,
        # which then decodes as garbage or raises on the padding.
        chunk = Image.open(io.BytesIO(png_bytes)).info.get("chara")
        assert chunk, "no chara chunk in the exported card"
        card = json.loads(base64.b64decode(chunk))
        return sorted(e["name"] for e in card["data"]["character_book"]["entries"])

    default = await client.get(f"/api/characters/{card_id}/export")
    assert _names(default.content) == ["The Bridge"]

    effective = await client.get(f"/api/characters/{card_id}/export", params={"world_view": "effective"})
    assert _names(effective.content) == ["Collapsed Bridge", "The Bridge"]


async def test_the_world_list_carries_the_awaiting_review_count(client, llm_mock):
    world_id, changeset = await _staged_proposal(client, llm_mock, "conv-dw-36")
    worlds = (await client.get("/api/worlds")).json()
    assert next(w["pending_changesets"] for w in worlds if w["id"] == world_id) == 1
    assert all(w["pending_changesets"] == 0 for w in worlds if w["id"] != world_id)

    # A stale proposal still needs a decision, so it stays in the badge; a
    # rejected one does not.
    await client.post(f"/api/worlds/{world_id}/entries", json={"name": "Late", "content": "x"})
    await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/apply", json={})
    worlds = (await client.get("/api/worlds")).json()
    assert next(w["pending_changesets"] for w in worlds if w["id"] == world_id) == 1

    await client.post(f"/api/worlds/{world_id}/changesets/{changeset['id']}/reject")
    worlds = (await client.get("/api/worlds")).json()
    assert next(w["pending_changesets"] for w in worlds if w["id"] == world_id) == 0
