"""The Character Library's auto-tagger, end to end.

The subject is the idempotency contract, which is the whole point of the
feature: pressing the button twice must not re-bill the library, and the only
things that legitimately make work appear are a new card, an edited card, and a
tag added to the vocabulary. Every test therefore asserts on the *number of
model calls*, not just on the rows that came out.
"""

from __future__ import annotations

import json

import pytest


def _tag_call(tags: list[str]) -> list[dict]:
    """An ``assign_character_tags`` tool call in the OpenAI wire shape."""
    return [
        {
            "id": "call-1",
            "type": "function",
            "function": {"name": "assign_character_tags", "arguments": json.dumps({"tags": tags})},
        }
    ]


def _parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        ev: dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = line[6:].replace("\\n", "\n")
        events.append(ev)
    return events


async def _cards(client, *names: str) -> list[str]:
    ids = []
    for name in names:
        r = await client.post("/api/characters", json={"name": name, "description": f"{name} wanders the wood."})
        ids.append(r.json()["id"])
    return ids


async def _vocab(client, names: list[str]) -> dict:
    return (await client.put("/api/library/tags", json={"vocabulary": names})).json()


async def _tags(client, card_id: str) -> list[str]:
    """A card's tags as the rest of the app sees them — there is only one list."""
    return (await client.get(f"/api/characters/{card_id}")).json()["tags"]


async def _run(client, llm_mock, answers: list[list[str]] | None = None, **body) -> list[dict]:
    """Drive one full run, queuing *answers* (one per expected call)."""
    for tags in answers or []:
        llm_mock.enqueue_auto_tag(_tag_call(tags))
    r = await client.post("/api/library/auto-tag/run", json=body or {})
    assert r.status_code == 200
    return _parse_sse(r.text)


def _auto_tag_calls(llm_mock) -> int:
    return sum(1 for name, _ in llm_mock.calls if name == "auto_tag")


# ── The contract ─────────────────────────────────────────────────────────────


async def test_a_run_tags_every_card_and_a_second_run_costs_nothing(client, llm_mock):
    ids = await _cards(client, "Lira", "Rook", "Zara")
    state = await _vocab(client, ["Fantasy", "Romance"])
    assert state["pending"] == 3 and state["total"] == 3

    events = await _run(client, llm_mock, [["Fantasy"], ["Fantasy", "Romance"], []])
    done = [e for e in events if e["event"] == "done"]
    assert json.loads(done[0]["data"]) == {"tagged": 3, "failed": 0}
    assert _auto_tag_calls(llm_mock) == 3

    assert (await client.get("/api/library/tags")).json()["pending"] == 0
    # Newest card first, so the answers land in reverse creation order. The empty
    # one was stored, not skipped — otherwise that card is pending forever and
    # re-billed on every run.
    assert [await _tags(client, cid) for cid in ids] == [[], ["Fantasy", "Romance"], ["Fantasy"]]

    # Second press: nothing pending, so nothing reaches the model.
    events = await _run(client, llm_mock)
    assert json.loads([e for e in events if e["event"] == "done"][0]["data"]) == {"tagged": 0, "failed": 0}
    assert _auto_tag_calls(llm_mock) == 3


async def test_a_new_card_is_the_only_pending_one(client, llm_mock):
    await _cards(client, "Lira")
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])

    (new_id,) = await _cards(client, "Rook")
    assert (await client.get("/api/library/tags")).json()["pending"] == 1

    await _run(client, llm_mock, [["Fantasy"]])
    assert _auto_tag_calls(llm_mock) == 2
    assert (await client.get("/api/library/tags")).json()["pending"] == 0
    assert await _tags(client, new_id) == ["Fantasy"]


async def test_editing_a_card_makes_exactly_that_card_pending(client, llm_mock):
    lira, rook = await _cards(client, "Lira", "Rook")
    await _vocab(client, ["Fantasy", "Romance"])
    await _run(client, llm_mock, [["Fantasy"], ["Fantasy"]])

    # updated_at is the content fingerprint: only the PUT route writes it — a
    # tagging run writes tags and its two stamps but deliberately leaves
    # updated_at alone — so an edit is the one thing that can invalidate a card's
    # tags. A run that bumped it would make every card it just tagged pending.
    await client.put(f"/api/characters/{lira}", json={"description": "Lira has taken up the sword."})
    assert (await client.get("/api/library/tags")).json()["pending"] == 1

    await _run(client, llm_mock, [["Fantasy", "Romance"]])
    assert _auto_tag_calls(llm_mock) == 3
    assert (await client.get("/api/library/tags")).json()["pending"] == 0
    assert await _tags(client, lira) == ["Fantasy", "Romance"]
    assert await _tags(client, rook) == ["Fantasy"]


async def test_deleting_a_tag_strips_it_everywhere_with_no_model_calls(client, llm_mock):
    lira, rook = await _cards(client, "Lira", "Rook")
    await _vocab(client, ["Fantasy", "Romance"])
    await _run(client, llm_mock, [["Romance"], ["Fantasy", "Romance"]])
    assert _auto_tag_calls(llm_mock) == 2

    state = await _vocab(client, ["Fantasy"])
    # Pruned, restamped, and nothing left to do: no card can have gained a tag it
    # was never offered, so every stored answer is still correct.
    assert state["vocabulary"] == ["Fantasy"]
    assert state["pending"] == 0
    assert await _tags(client, lira) == ["Fantasy"]
    assert await _tags(client, rook) == []
    assert _auto_tag_calls(llm_mock) == 2


async def test_deleting_a_tag_leaves_an_untagged_card_alone(client, llm_mock):
    """Pruning is scoped to cards a run has written.

    A card the tagger has never touched still carries whatever it was imported
    with, and removing a vocabulary name that happens to collide with one of
    those must not reach in and delete it.
    """
    r = await client.post("/api/characters", json={"name": "Lira", "tags": ["Fantasy", "tavern"]})
    card_id = r.json()["id"]
    await _vocab(client, ["Fantasy", "Romance"])
    await _vocab(client, ["Romance"])

    assert await _tags(client, card_id) == ["Fantasy", "tavern"]
    assert _auto_tag_calls(llm_mock) == 0


async def test_reordering_the_vocabulary_costs_nothing(client, llm_mock):
    await _cards(client, "Lira")
    await _vocab(client, ["Fantasy", "Romance"])
    await _run(client, llm_mock, [["Fantasy"]])

    state = await _vocab(client, ["Romance", "Fantasy"])
    assert state["vocabulary"] == ["Romance", "Fantasy"]
    assert state["pending"] == 0
    assert _auto_tag_calls(llm_mock) == 1


# Reordering the vocabulary rewrites the system prompt, so the second run's
# prefix legitimately differs from the first's — the same class as
# ``test_adding_a_tag_makes_every_card_pending`` below. Within each run the
# prefix is still constant.
@pytest.mark.kv_divergence_expected
async def test_a_reorder_does_not_mark_an_untagged_card_current(client, llm_mock):
    """The hash bump restamps tagged cards only.

    Restamping every row would mark a card that was never sent to the model as
    up to date against the new vocabulary, and it would never be tagged.
    """
    await _cards(client, "Lira", "Rook")
    await _vocab(client, ["Fantasy", "Romance"])
    await _run(client, llm_mock, [["Fantasy"], ["Romance"]])

    (untagged,) = await _cards(client, "Zara")
    assert (await _vocab(client, ["Romance", "Fantasy"]))["pending"] == 1

    await _run(client, llm_mock, [["Romance"]])
    assert _auto_tag_calls(llm_mock) == 3
    assert await _tags(client, untagged) == ["Romance"]


# Two runs against two different vocabularies, so the run's shared prefix
# legitimately differs between them — the same class as a persona switch
# mid-conversation, and the reason the opt-out exists. Within each run the prefix
# is still constant, which is what ``test_a_multi_card_run_ships_one_prefix``
# checks.
@pytest.mark.kv_divergence_expected
async def test_adding_a_tag_makes_every_card_pending(client, llm_mock):
    await _cards(client, "Lira", "Rook")
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"], []])

    state = await _vocab(client, ["Fantasy", "Sci-Fi"])
    assert state["pending"] == 2

    await _run(client, llm_mock, [["Fantasy"], ["Sci-Fi"]])
    assert _auto_tag_calls(llm_mock) == 4
    assert (await client.get("/api/library/tags")).json()["pending"] == 0


async def test_a_mid_run_failure_keeps_earlier_work_and_leaves_that_card_pending(client, llm_mock, db):
    await _cards(client, "Lira", "Rook", "Zara")
    await _vocab(client, ["Fantasy"])

    # An answer with no tool call: the endpoint replied, but not with tags.
    # Nothing is written, so the card stays pending and the next press retries it.
    llm_mock.enqueue_auto_tag(_tag_call(["Fantasy"]))
    llm_mock.enqueue_auto_tag([])
    llm_mock.enqueue_auto_tag(_tag_call([]))
    events = _parse_sse((await client.post("/api/library/auto-tag/run", json={})).text)

    assert [e["event"] for e in events if e["event"] == "card_error"] == ["card_error"]
    assert json.loads([e for e in events if e["event"] == "done"][0]["data"]) == {"tagged": 2, "failed": 1}

    async with db.execute("SELECT COUNT(*) AS n FROM character_cards WHERE auto_tag_vocab_hash != ''") as cur:
        assert (await cur.fetchone())["n"] == 2
    assert (await client.get("/api/library/tags")).json()["pending"] == 1

    # The retry touches only the card that failed.
    await _run(client, llm_mock, [["Fantasy"]])
    assert _auto_tag_calls(llm_mock) == 4
    assert (await client.get("/api/library/tags")).json()["pending"] == 0


async def test_the_run_stops_after_five_consecutive_failures(client, llm_mock):
    await _cards(client, *[f"Card{i}" for i in range(12)])
    await _vocab(client, ["Fantasy"])
    for _ in range(12):
        llm_mock.enqueue_auto_tag([])
    events = _parse_sse((await client.post("/api/library/auto-tag/run", json={})).text)

    # A dead endpoint costs a handful of calls, not the whole library.
    assert _auto_tag_calls(llm_mock) == 5
    assert [e["event"] for e in events][-1] == "error"


async def test_a_run_with_no_vocabulary_is_refused_before_any_call(client, llm_mock):
    await _cards(client, "Lira")
    events = _parse_sse((await client.post("/api/library/auto-tag/run", json={})).text)
    assert [e["event"] for e in events] == ["error"]
    assert _auto_tag_calls(llm_mock) == 0


# ── Storage ──────────────────────────────────────────────────────────────────


async def test_a_run_writes_the_cards_own_tags_column(client, llm_mock, db):
    """One store, not two: the tagger writes the column every other reader uses."""
    (card_id,) = await _cards(client, "Lira")
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])

    async with db.execute("SELECT tags, auto_tag_vocab_hash FROM character_cards WHERE id = ?", (card_id,)) as cur:
        row = await cur.fetchone()
    assert json.loads(row["tags"]) == ["Fantasy"]
    assert row["auto_tag_vocab_hash"]


async def test_tagging_a_card_overwrites_the_tags_it_was_imported_with(client, llm_mock):
    """The accepted cost of one tag store. Importer noise is what the vocabulary replaces."""
    r = await client.post("/api/characters", json={"name": "Lira", "tags": ["TAVERN", "anypov"]})
    card_id = r.json()["id"]
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])

    assert await _tags(client, card_id) == ["Fantasy"]


async def test_the_vocabulary_is_normalized_on_save(client):
    state = await _vocab(client, ["  Fantasy ", "fantasy", "Sci|Fi", ""])
    assert state["vocabulary"] == ["Fantasy", "SciFi"]


# ── KV posture ───────────────────────────────────────────────────────────────


async def test_a_multi_card_run_ships_one_prefix(client, llm_mock):
    """The batch lane's whole reason for being sequential and single-prefix.

    ``verify_kv_prefix_invariants`` asserts this at teardown for every test in
    this file (see ``_BATCH_PASSES``); this one states it directly so a reader
    can see what the run is supposed to look like on the wire.
    """
    await _cards(client, "Lira", "Rook", "Zara")
    await _vocab(client, ["Fantasy", "Romance"])
    await _run(client, llm_mock, [["Fantasy"], [], ["Romance"]])

    calls = [c for c in llm_mock.captured if c["pass"] == "auto_tag"]
    assert len(calls) == 3
    assert len({json.dumps(c["messages"][0], sort_keys=True) for c in calls}) == 1
    assert len({json.dumps(c["tools"], sort_keys=True) for c in calls}) == 1
    # Only the card's own text moves between calls.
    assert len({json.dumps(c["messages"][1], sort_keys=True) for c in calls}) == 3


# ── thinking ─────────────────────────────────────────────────────────────────


async def test_a_run_pins_thinking_off_by_default(client, llm_mock):
    await _cards(client, "Lira")
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])

    params = [c["params"] for c in llm_mock.captured if c["pass"] == "auto_tag"]
    assert params and all(p["chat_template_kwargs"] == {"enable_thinking": False, "thinking": False} for p in params)


async def test_the_run_body_can_turn_thinking_on(client, llm_mock):
    await _cards(client, "Lira", "Rook")
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"], []], reasoning=True)

    params = [c["params"] for c in llm_mock.captured if c["pass"] == "auto_tag"]
    assert len(params) == 2
    # Pinned once for the whole run, so the shared prefix stays shared.
    assert all(p["chat_template_kwargs"] == {"enable_thinking": True, "thinking": True} for p in params)


# ── Recoverability ───────────────────────────────────────────────────────────
# The run rewrites a column that leaves this install: ``tags`` is what ``to_png``
# writes into an exported card's V2/V3 chunk. These pin the way back.


def _exported_tags(png: bytes) -> list[str]:
    """The ``tags`` of the V2 chunk in an exported card PNG."""
    import base64
    import re

    blob = re.search(rb"chara\x00([A-Za-z0-9+/=]+)", png)
    assert blob, "no chara chunk in the exported PNG"
    payload = json.loads(base64.b64decode(blob.group(1)))
    return payload.get("data", payload)["tags"]


async def test_an_export_carries_the_creators_tags_not_the_librarys(client, llm_mock):
    """The owner's private vocabulary must not leave with a shared card."""
    card_id = (await client.post("/api/characters", json={"name": "Lira", "tags": ["anypov", "oc"]})).json()["id"]
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])

    assert await _tags(client, card_id) == ["Fantasy"]  # the library sees the curated list
    assert _exported_tags((await client.get(f"/api/characters/{card_id}/export")).content) == ["anypov", "oc"]


async def test_restore_puts_the_imported_tags_back_and_makes_the_card_pending(client, llm_mock):
    card_id = (await client.post("/api/characters", json={"name": "Lira", "tags": ["anypov"]})).json()["id"]
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])

    state = (await client.post("/api/library/auto-tag/restore", json={})).json()
    assert state["restored"] == 1
    assert await _tags(client, card_id) == ["anypov"]
    # A toggle, not a trapdoor: the card is the tagger's business again.
    assert state["pending"] == 1 and state["restorable"] == 0


# Two runs under two vocabularies: the second prefix legitimately differs from
# the first, which is a user-driven invalidation and not the drift the checker
# hunts. Within each run the prefix is still one — see the KV posture block.
@pytest.mark.kv_divergence_expected
async def test_a_second_run_does_not_overwrite_the_stash(client, llm_mock):
    """``imported_tags`` is what the card arrived with, not the previous answer."""
    card_id = (await client.post("/api/characters", json={"name": "Lira", "tags": ["anypov"]})).json()["id"]
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])
    await _vocab(client, ["Fantasy", "Romance"])  # an addition re-pends every card
    await _run(client, llm_mock, [["Romance"]])

    assert await _tags(client, card_id) == ["Romance"]
    await client.post("/api/library/auto-tag/restore", json={})
    assert await _tags(client, card_id) == ["anypov"]


async def test_a_card_that_arrived_untagged_still_restores(client, llm_mock):
    """``''`` means untouched and ``'[]'`` means arrived bare — the two must not merge."""
    card_id = (await client.post("/api/characters", json={"name": "Lira"})).json()["id"]
    await _vocab(client, ["Fantasy"])
    await _run(client, llm_mock, [["Fantasy"]])

    assert (await client.post("/api/library/auto-tag/restore", json={})).json()["restored"] == 1
    assert await _tags(client, card_id) == []


# ── The panel's arithmetic ───────────────────────────────────────────────────


async def test_an_empty_vocabulary_reports_no_work(client):
    """Or the panel offers a run whose only outcome is being refused."""
    await _cards(client, "Lira", "Rook")
    state = (await client.get("/api/library/tags")).json()
    assert state["vocabulary"] == [] and state["total"] == 2
    assert state["pending"] == 0
