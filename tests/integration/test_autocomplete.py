"""Autocomplete route: 503 when the model is unavailable, 200 with a completion
otherwise. The model itself is monkeypatched — no GGUF needed here."""

from __future__ import annotations

import backend.database as dbmod


async def test_autocomplete_503_when_unavailable(client, monkeypatch):
    monkeypatch.setattr(
        "backend.inference.local_ml.available",
        lambda: (False, "extra not installed"),
    )
    await dbmod.create_conversation("conv-ac", "Chat", "Nova", "")
    resp = await client.post("/api/conversations/conv-ac/autocomplete", json={"draft": "hello"})
    assert resp.status_code == 503
    assert "unavailable" in resp.json()["detail"].lower()


async def test_autocomplete_returns_completion(client, monkeypatch):
    monkeypatch.setattr("backend.inference.local_ml.available", lambda: (True, ""))

    async def fake_complete(prompt, *a, **k):
        assert "Nova" in prompt  # char name threaded into the trimmed prompt
        assert prompt.endswith("I walk into the")  # draft is the trailing line
        return " tavern and look around."

    monkeypatch.setattr("backend.features.autocomplete.complete", fake_complete)
    await dbmod.create_conversation("conv-ac2", "Chat", "Nova", "")
    mid, _ = await dbmod.add_message("conv-ac2", "assistant", "You arrive at the gate.", 0, parent_id=None)
    await dbmod.set_active_leaf("conv-ac2", mid)

    resp = await client.post("/api/conversations/conv-ac2/autocomplete", json={"draft": "I walk into the"})
    assert resp.status_code == 200
    assert resp.json()["completion"] == " tavern and look around."


async def test_autocomplete_blank_draft_skips_model(client, monkeypatch):
    monkeypatch.setattr("backend.inference.local_ml.available", lambda: (True, ""))

    async def boom(*a, **k):
        raise AssertionError("model must not be called for a blank draft")

    monkeypatch.setattr("backend.features.autocomplete.complete", boom)
    await dbmod.create_conversation("conv-ac3", "Chat", "Nova", "")

    resp = await client.post("/api/conversations/conv-ac3/autocomplete", json={"draft": "   "})
    assert resp.status_code == 200
    assert resp.json()["completion"] == ""


async def test_autocomplete_in_a_group_names_the_speaker_and_the_cast(client, monkeypatch):
    """A group has no card to describe and no single voice to attribute replies
    to, so the typeahead reads the roster and labels each line by its member."""
    monkeypatch.setattr("backend.inference.local_ml.available", lambda: (True, ""))
    captured: dict[str, str] = {}

    async def fake_complete(prompt, *a, **k):
        captured["prompt"] = prompt
        return " toward the fire."

    monkeypatch.setattr("backend.features.autocomplete.complete", fake_complete)
    aria = (await client.post("/api/characters", json={"name": "Aria"})).json()["id"]
    kael = (await client.post("/api/characters", json={"name": "Kael"})).json()["id"]
    conv = (
        await client.post(
            "/api/conversations",
            json={
                "kind": "group",
                "title": "Campfire",
                "members": [{"character_card_id": aria}, {"character_card_id": kael}],
            },
        )
    ).json()
    members = (await client.get(f"/api/conversations/{conv['id']}/members")).json()
    mid, _ = await dbmod.add_message(conv["id"], "assistant", "The fire gutters.", 0, speaker_member_id=members[1]["id"])
    await dbmod.set_active_leaf(conv["id"], mid)

    resp = await client.post(f"/api/conversations/{conv['id']}/autocomplete", json={"draft": "I step"})
    assert resp.status_code == 200
    prompt = captured["prompt"]
    assert "Scene cast: Aria, Kael" in prompt
    assert "Kael: The fire gutters." in prompt
    assert "Campfire: The fire gutters." not in prompt
    assert prompt.endswith("I step")


async def test_autocomplete_leaves_the_cast_macro_alone_in_a_solo_chat(client, monkeypatch):
    """`{{cast}}` is a group macro: the turn itself fills it only in a group, so
    filling it here would have a draft preview one thing and send another."""
    monkeypatch.setattr("backend.inference.local_ml.available", lambda: (True, ""))
    captured: dict[str, str] = {}

    async def fake_complete(prompt, *a, **k):
        captured["prompt"] = prompt
        return " ..."

    monkeypatch.setattr("backend.features.autocomplete.complete", fake_complete)
    await dbmod.create_conversation("conv-ac-solo-cast", "Chat", "Nova", "")

    resp = await client.post("/api/conversations/conv-ac-solo-cast/autocomplete", json={"draft": "I ask {{cast}} about"})
    assert resp.status_code == 200
    assert captured["prompt"].endswith("I ask {{cast}} about")


async def test_autocomplete_unknown_conversation_404(client):
    resp = await client.post("/api/conversations/nope/autocomplete", json={"draft": "hi"})
    assert resp.status_code == 404
