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

    monkeypatch.setattr("backend.inference.local_ml.complete", fake_complete)
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

    monkeypatch.setattr("backend.inference.local_ml.complete", boom)
    await dbmod.create_conversation("conv-ac3", "Chat", "Nova", "")

    resp = await client.post("/api/conversations/conv-ac3/autocomplete", json={"draft": "   "})
    assert resp.status_code == 200
    assert resp.json()["completion"] == ""


async def test_autocomplete_unknown_conversation_404(client):
    resp = await client.post("/api/conversations/nope/autocomplete", json={"draft": "hi"})
    assert resp.status_code == 404
