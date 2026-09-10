"""The Character Library duplicate finder, from scan through guarded removal."""

from __future__ import annotations

import json

import backend.api.routes.library as library_routes
from backend.database import apply_auto_tags, replace_vocabulary
from backend.features.library_tags import vocabulary_hash

_PNG_1X1_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
_BODY = " ".join(
    [
        "Mara charts the riverbanks at dawn and listens for a bell beneath the fog.",
        "She keeps careful maps, asks patient questions, and refuses to leave a traveler behind.",
    ]
    * 8
)


def _events(text: str) -> list[tuple[str, object]]:
    """Decode the small SSE vocabulary the duplicate scan emits."""
    events: list[tuple[str, object]] = []
    for block in text.strip().split("\n\n"):
        name = ""
        payload: object = ""
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                raw = line[6:]
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    payload = raw
        if name:
            events.append((name, payload))
    return events


async def _card(client, name: str, **extra) -> str:
    response = await client.post(
        "/api/characters", json={"name": name, "description": _BODY, "first_mes": "Welcome home.", **extra}
    )
    assert response.status_code == 200
    return response.json()["id"]


async def _scan(client) -> dict:
    response = await client.post("/api/library/duplicates/scan")
    assert response.status_code == 200
    done = [data for name, data in _events(response.text) if name == "done"]
    assert len(done) == 1
    assert isinstance(done[0], dict)
    return done[0]


def _strong_pair(report: dict, a: str, b: str) -> dict:
    expected = {a, b}
    for group in report["groups"]:
        if set(group["cards"]) == expected:
            return group["pairs"][0]
    raise AssertionError("the scan did not return the expected strong group")


# ── Scan cache and dismissals ───────────────────────────────────────────────


async def test_a_scan_finds_two_cards_that_differ_only_in_tags(client):
    """Organizational tags must neither block content matching nor leave the server."""
    left = await _card(client, "Mara", tags=["favorite"])
    right = await _card(client, "Mara", tags=["imported", "archive"])

    report = await _scan(client)

    pair = _strong_pair(report, left, right)
    assert pair["reasons"][0] == "Identical content"
    assert "description" not in json.dumps(report)
    assert {item["id"] for item in report["cards"]} == {left, right}


async def test_a_second_scan_rehashes_no_avatars(client, monkeypatch):
    """The dHash cache avoids a second expensive Pillow decode for unchanged art."""
    await _card(client, "Mara", avatar_b64=_PNG_1X1_B64, avatar_mime="image/png")
    calls = 0
    original = library_routes.dhash_from_image_bytes

    def counted(data: bytes) -> str:
        nonlocal calls
        calls += 1
        return original(data)

    monkeypatch.setattr(library_routes, "dhash_from_image_bytes", counted)
    assert (await _scan(client))["stats"]["avatar_rehashed"] == 1
    assert calls == 1

    assert (await _scan(client))["stats"]["avatar_rehashed"] == 0
    assert calls == 1


async def test_a_scan_does_not_make_the_library_pending_for_retagging(client):
    """Hash-cache writes must leave ``updated_at`` alone, preserving tag stamps."""
    card_id = await _card(client, "Mara", avatar_b64=_PNG_1X1_B64, avatar_mime="image/png")
    vocabulary = ["Fantasy"]
    await replace_vocabulary(vocabulary, expected=[], new_hash=vocabulary_hash(vocabulary))
    card = (await client.get(f"/api/characters/{card_id}")).json()
    assert await apply_auto_tags(card_id, ["Fantasy"], vocabulary_hash(vocabulary), card["updated_at"])
    assert (await client.get("/api/library/tags")).json()["pending"] == 0

    await _scan(client)

    assert (await client.get("/api/library/tags")).json()["pending"] == 0


async def test_a_dismissed_pair_stops_being_reported_and_an_edit_reopens_it(client):
    """Dismissals are pair-local and lapse only when meaningful card content changes."""
    left = await _card(client, "Mara")
    right = await _card(client, "Mara")
    report = await _scan(client)
    pair = _strong_pair(report, left, right)

    dismissed = await client.post("/api/library/duplicates/dismiss", json={"pairs": [[pair["a"], pair["b"]]]})
    assert dismissed.status_code == 200 and dismissed.json() == {"dismissed": 1}
    hidden = await _scan(client)
    assert hidden["groups"] == [] and hidden["pairs"] == []

    # Scenario is meaningful identity content but not a shingle field. The
    # cards remain a strong description+greeting match while their dismissal
    # body hashes now differ.
    assert (await client.put(f"/api/characters/{left}", json={"scenario": "The fog has lifted."})).status_code == 200
    reopened = await _scan(client)
    assert _strong_pair(reopened, left, right)
    assert reopened["stats"]["reopened"] == 1


async def test_retagging_a_dismissed_card_does_not_bring_the_pair_back(client):
    """Tags are deliberately outside a dismissal's body-hash stamp."""
    left = await _card(client, "Mara")
    right = await _card(client, "Mara")
    pair = _strong_pair(await _scan(client), left, right)
    await client.post("/api/library/duplicates/dismiss", json={"pairs": [[pair["a"], pair["b"]]]})

    assert (await client.put(f"/api/characters/{left}", json={"tags": ["favorite"]})).status_code == 200
    hidden = await _scan(client)

    assert hidden["groups"] == [] and hidden["stats"]["dismissed"] == 1


# ── Guarded resolution ──────────────────────────────────────────────────────


async def test_deleting_a_card_with_conversations_is_refused_without_relink(client):
    """A no-relink delete never silently turns a live conversation into an orphan."""
    keep = await _card(client, "Mara")
    remove = await _card(client, "Mara")
    conversation = await client.post("/api/conversations", json={"character_card_id": remove})
    assert conversation.status_code == 200

    response = await client.post(
        "/api/library/duplicates/resolve", json={"keep_id": keep, "remove_id": remove, "relink": False}
    )

    assert response.status_code == 409
    assert response.json()["detail"]["impact"]["conversations"] == 1
    assert (await client.get(f"/api/characters/{remove}")).status_code == 200


async def test_relinking_moves_conversations_to_the_keeper(client):
    """Relinking rewrites both the card id and denormalized keeper fields."""
    keep = await _card(client, "Keeper", scenario="Keeper scenario")
    remove = await _card(client, "Doomed", scenario="Doomed scenario")
    conversation = (await client.post("/api/conversations", json={"character_card_id": remove})).json()

    response = await client.post("/api/library/duplicates/resolve", json={"keep_id": keep, "remove_id": remove, "relink": True})

    assert response.status_code == 200 and response.json()["impact"]["conversations"] == 1
    moved = next(item for item in (await client.get("/api/conversations")).json() if item["id"] == conversation["id"])
    assert moved["character_card_id"] == keep
    assert moved["character_name"] == "Keeper" and moved["character_scenario"] == "Keeper scenario"
    assert (await client.get(f"/api/characters/{remove}")).status_code == 404


async def test_relinking_a_group_chat_that_already_casts_the_keeper_drops_the_duplicate_slot(client):
    """The active-member partial unique index is respected instead of raising."""
    keep = await _card(client, "Keeper")
    remove = await _card(client, "Doomed")
    conversation = (
        await client.post(
            "/api/conversations",
            json={"kind": "group", "members": [{"character_card_id": keep}, {"character_card_id": remove}]},
        )
    ).json()

    response = await client.post("/api/library/duplicates/resolve", json={"keep_id": keep, "remove_id": remove, "relink": True})

    assert response.status_code == 200 and response.json()["impact"]["collisions"] == 1
    members = (await client.get(f"/api/conversations/{conversation['id']}/members")).json()
    assert [member["character_card_id"] for member in members] == [keep]


async def test_a_scan_cannot_start_underneath_a_tagging_run(client):
    """Tagging and duplicate scans share the same card-write lock."""
    async with library_routes._run_lock:
        response = await client.post("/api/library/duplicates/scan")

    events = _events(response.text)
    assert response.status_code == 200
    assert events == [("error", "The library is busy — a tagging run or another duplicate scan is already under way")]
