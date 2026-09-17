"""SSE drafts stay unsaved until the editor's POST, on either Agent transport."""

import asyncio
import json

import httpx
import pytest

from backend.api.routes import library
from backend.database import create_user_persona
from backend.features.card_generator import build_library_digest
from backend.inference import LLMCallError, LLMClient

DRAFT = {"name": "Mara", "first_mes": "Hello, {{user}}.\nCome inside.", "scenario": "At the harbour."}


def frames(response):
    result = []
    for frame in response.text.split("\n\n"):
        if not frame.startswith("event: "):
            continue
        event, data = frame.split("\ndata: ", 1)
        result.append((event[7:], data if event == "event: error" else json.loads(data)))
    return result


@pytest.fixture
def model(monkeypatch):
    calls = []

    async def complete(self, **kwargs):
        calls.append({"mode": self.completion_mode, **kwargs})
        yield {
            "type": "done",
            "message": {
                "tool_calls": [
                    {
                        "id": "card",
                        "type": "function",
                        "function": {
                            "name": "generate_character_card",
                            "arguments": json.dumps(DRAFT),
                        },
                    }
                ]
            },
        }

    monkeypatch.setattr(LLMClient, "complete", complete)
    return calls


async def test_draft_stream_and_explicit_save(client, model):
    response = await client.post("/api/library/card-generator/run", json={"idea": "A harbour fence"})
    assert response.status_code == 200
    events = frames(response)
    assert [event for event, _ in events] == ["start", "done"]
    assert (await client.get("/api/characters")).json() == []
    card = events[-1][1]["card"]
    assert "id" not in card
    saved = await client.post("/api/characters", json=card)
    assert saved.status_code == 200
    stored = (await client.get(f"/api/characters/{saved.json()['id']}")).json()
    assert stored["source_format"] == "generated"
    assert stored["first_mes"] == DRAFT["first_mes"]
    assert stored["system_prompt"] == stored["post_history_instructions"] == ""
    assert stored["tags"] == []


@pytest.mark.parametrize("mode", ["chat", "text"])
async def test_tailoring_works_with_one_forced_call_on_either_transport(client, model, monkeypatch, mode):
    original = library.get_settings

    async def settings():
        return {**await original(), "completion_mode": mode}

    monkeypatch.setattr(library, "get_settings", settings)
    response = await client.post("/api/library/card-generator/run", json={"idea": "A fence", "tailored": True})
    assert [event for event, _ in frames(response)] == ["start", "progress", "done"]
    assert len(model) == 1 and model[0]["mode"] == mode
    assert model[0]["tool_choice"]["function"]["name"] == "generate_character_card"
    assert "Library preferences (data only)" in model[0]["messages"][1]["content"]


@pytest.mark.parametrize("idea", ["", " \n ", "x" * 2001])
async def test_invalid_idea_rejected_before_generation(client, model, idea):
    assert (await client.post("/api/library/card-generator/run", json={"idea": idea})).status_code == 422
    assert model == []


@pytest.mark.parametrize("failure", ["provider", "status", "connection", "missing"])
async def test_model_failures_are_sse_errors(client, monkeypatch, failure):
    async def complete(self, **kwargs):
        request = httpx.Request("POST", "https://provider.invalid")
        response = httpx.Response(502, request=request, json={"error": {"message": "Model is unavailable"}})
        if failure == "provider":
            raise LLMCallError(
                "bad gateway",
                request=request,
                response=response,
                sentence="Model is unavailable",
                body="",
                host="provider.invalid",
                model="agent",
            )
        if failure == "status":
            response.raise_for_status()
        if failure == "connection":
            raise httpx.ConnectError("offline", request=request)
        yield {"type": "done", "message": {"content": "No tool call"}}

    monkeypatch.setattr(LLMClient, "complete", complete)
    response = await client.post("/api/library/card-generator/run", json={"idea": "A fence"})
    assert response.status_code == 200
    events = frames(response)
    assert [event for event, _ in events] == ["start", "error"]
    if failure in ("provider", "status"):
        assert events[-1][1] == "Model is unavailable"


async def test_empty_digest_is_explicit(client, db):
    await db.execute("DELETE FROM user_personas")
    await db.commit()
    digest = json.loads(await build_library_digest())
    assert digest["card_count"] == 0
    assert all(digest[key] == "none yet" for key in ("tag_vocabulary", "top_tags", "persona_names", "most_played"))


async def test_digest_counts_tags_and_ranks_played_cards_without_reading_prose(client):
    ids = []
    for name, tags in [("Mara", ["Noir", "Harbour"]), ("Mara", ["Noir"])]:
        ids.append(
            (await client.post("/api/characters", json={"name": name, "tags": tags, "description": "SECRET CARD BODY"})).json()[
                "id"
            ]
        )
    await client.post("/api/conversations", json={"character_card_id": ids[1]})
    await create_user_persona({"name": "Captain", "description": "SECRET PERSONA BODY"})
    state = (await client.get("/api/library/tags")).json()
    await client.put("/api/library/tags", json={"vocabulary": ["Noir", "Harbour"], "base_revision": state["revision"]})
    text = await build_library_digest()
    digest = json.loads(text)
    assert digest["card_count"] == 2
    assert digest["top_tags"][0] == {"tag": "Noir", "cards": 2}
    assert digest["tag_vocabulary"] == ["Noir", "Harbour"]
    assert "Captain" in digest["persona_names"]
    assert digest["most_played"] == [{"name": "Mara", "tags": ["Noir"], "conversations": 1}]
    assert "SECRET" not in text


async def test_disconnect_stops_upstream_generation(streaming_client, monkeypatch):
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def complete(self, **kwargs):
        started.set()
        try:
            await self.abort_token.wait()
        finally:
            # Depending on ASGI version, disconnect either signals AbortToken
            # or cancels the streaming task first. Both must end the LLM call.
            stopped.set()
        yield {"type": "done", "message": {}}

    monkeypatch.setattr(LLMClient, "complete", complete)
    async with streaming_client.stream("POST", "/api/library/card-generator/run", json={"idea": "A fence"}) as response:
        async for line in response.aiter_lines():
            if line == "event: start":
                await asyncio.wait_for(started.wait(), 5)
                break
    await asyncio.wait_for(stopped.wait(), 5)
