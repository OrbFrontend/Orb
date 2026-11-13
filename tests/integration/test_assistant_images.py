"""Standalone assistant pictures: no chat target, temporary retrieval, enablement and Stop."""

import asyncio
import uuid
from collections import OrderedDict

import pytest

from backend.api import deps
from backend.api.assistant import images
from backend.database import get_character_avatar, get_character_card, message_workflow_attachment_ids, set_workflow_enabled
from backend.workflows import HookType, StandaloneResult, Workflow
from backend.workflows.registry import subscription
from tests.integration.test_assistant import RUN, _call, _reply, _script, build_library, frames
from tests.integration.workflows._fixtures import register_for_test


def _workflow(hook):
    return Workflow(id="image_gen", display_name="Image Generation", subscriptions=[subscription(HookType.STANDALONE, hook)])


def _request(**context):
    return {"text": "Make a portrait for an avatar.", "session": uuid.uuid4().hex, "context": {"mode": "library", **context}}


def _results(events):
    return [data for event, data in events if event == "tool_result"]


async def test_picture_without_a_chat_is_retrievable_and_leaves_cards_and_messages_alone(client, db, monkeypatch):
    library = await build_library(client, db)
    before = await get_character_card(library["card"])
    seen = []

    async def render(ctx):
        seen.append(ctx)
        ctx.phase("Rendering portrait…")
        assert not deps._workflow_jobs, "standalone renders have no conversation-keyed job"
        return StandaloneResult(b"picture", "image/png")

    with register_for_test(_workflow(render)):
        _script(
            monkeypatch,
            _call("create_image", {"description": " A harbour fence in a blue coat. ", "character_id": library["card"]}),
            _reply("Download it to set your avatar."),
        )
        events = frames(await client.post_checked(RUN, json=_request()))
        [result] = _results(events)
        assert result["ok"] is True
        image_id = result["ids"]["image_id"]
        assert ("progress", {"label": "Rendering portrait…"}) in events
        assert not [event for event, _ in events if event == "mutation"]
        ctx = seen[0]
        assert ctx.description == "A harbour fence in a blue coat."
        assert ctx.character_id == library["card"] and ctx.character["name"] == "Mara"
        assert not hasattr(ctx, "conversation_id") and not hasattr(ctx, "history")
        with pytest.raises(TypeError):
            ctx.character["name"] = "changed"
        await set_workflow_enabled("image_gen", False)
        picture = await client.get(f"/api/assistant/images/{image_id}")
        assert picture.status_code == 200 and picture.content == b"picture"
        assert picture.headers["content-type"] == "image/png"
        assert picture.headers["x-content-type-options"] == "nosniff"
        download = await client.get(f"/api/assistant/images/{image_id}?download=true")
        assert download.headers["content-disposition"] == 'attachment; filename="generated-image.png"'
        _script(monkeypatch, _call("create_image", {"description": "Another portrait."}), _reply("Switched off."))
        [refused] = _results(frames(await client.post_checked(RUN, json=_request())))
        assert not refused["ok"] and "`workflow:image_gen`" in refused["summary"]
        assert len(seen) == 1
    assert await get_character_card(library["card"]) == before
    assert await message_workflow_attachment_ids(library["kept"], "image_gen") == []
    assert (await client.get("/api/assistant/images/missing")).status_code == 404


async def test_no_implicit_character_and_errors_are_tool_failures(client, db, monkeypatch):
    library = await build_library(client, db)
    seen = []

    async def render(ctx):
        seen.append(ctx.character_id)
        return StandaloneResult(b"picture", "image/webp")

    with register_for_test(_workflow(render)):
        _script(
            monkeypatch,
            _call("create_image", {"description": "A lighthouse."}),
            _call("create_image", {"description": "Missing card.", "character_id": "missing"}),
            _call("create_image", {"description": "   "}),
            _reply("Done."),
        )
        events = frames(await client.post_checked(RUN, json=_request(character_id=library["card"])))
    assert seen == [None]
    results = _results(events)
    assert [result["ok"] for result in results] == [True, False, False]
    assert "character no longer exists" in results[1]["summary"]
    assert not results[2]["ids"]


async def test_stop_awaits_standalone_cleanup_and_keeps_no_picture(client, db, monkeypatch):
    await build_library(client, db)
    started, cleaned = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(images, "_images", OrderedDict())

    async def render(ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return StandaloneResult(b"never", "image/png")

    with register_for_test(_workflow(render)):
        _script(monkeypatch, _call("create_image", {"description": "A portrait."}))
        task = asyncio.create_task(client.post(RUN, json=_request()))
        await asyncio.wait_for(started.wait(), 5)
        stopped = await client.post_json("/api/assistant/stop?operation_id=op-1")
        events = frames(await asyncio.wait_for(task, 5))
    assert stopped["settled"] and cleaned.is_set()
    [result] = _results(events)
    assert not result["ok"] and "Stopped" in result["summary"]
    assert result["ids"] == {} and not images._images
    assert events[-1] == ("done", {"reason": "stopped", "message": ""})


async def test_failed_render_does_not_offer_an_image(client, db, monkeypatch):
    await build_library(client, db)
    from backend.workflows.errors import WorkflowUserFacingError

    async def render(ctx):
        raise WorkflowUserFacingError("Image server is unavailable.")

    with register_for_test(_workflow(render)):
        _script(monkeypatch, _call("create_image", {"description": "A portrait."}), _reply("Try again later."))
        [result] = _results(frames(await client.post_checked(RUN, json=_request())))
    assert not result["ok"] and result["summary"] == "Image server is unavailable."
    assert result["ids"] == {}


async def test_create_character_then_generate_avatar_in_the_next_request(client, db, monkeypatch):
    """Use the real image hook and prompter dispatch after the assistant creates a card in the preceding request."""
    from backend.workflows import set_workflow_config
    from backend.workflows.image_gen.engine import ImageResult
    from tests.integration.workflows.image_gen.test_hooks import CONFIG

    await build_library(client, db)
    await set_workflow_config("image_gen", {**CONFIG, "scene_skills_enabled": True, "refine_turns": 5})
    body = _request()
    body["text"] = "Create Iris, a silver-haired navigator."
    _script(
        monkeypatch,
        _call("create_character", {"name": "Iris", "description": "A silver-haired navigator."}),
        _reply("Created Iris."),
    )
    created = _results(frames(await client.post_checked(RUN, json=body)))[0]
    card_id = created["ids"]["character_id"]
    renders = []

    async def render(adapter, request, **kwargs):
        renders.append(request)
        return ImageResult(b"avatar picture", "image/png", {})

    monkeypatch.setattr("backend.workflows.image_gen.standalone.resolve_and_generate", render)
    _script(
        monkeypatch,
        _call("read_character", {"character_id": card_id}),
        _call(
            "create_image", {"description": "An avatar portrait of Iris, a silver-haired navigator.", "character_id": card_id}
        ),
        _call(
            "compose_image_prompt",
            {"scene": "1girl, solo, silver hair, navigator, portrait", "unwanted": "", "visible_subjects": ["Iris"]},
        ),
        _reply("Download the picture, then set and crop it as Iris's avatar."),
    )
    body["text"] = "Now make her an avatar."
    events = frames(await client.post_checked(RUN, json=body))
    assert events[0] == ("start", {"continued": True})
    image_result = _results(events)[1]
    assert image_result["ok"] is True and image_result["tool"] == "create_image"
    response = await client.get(f"/api/assistant/images/{image_result['ids']['image_id']}")
    assert response.content == b"avatar picture" and len(renders) == 1
    assert await get_character_avatar(card_id) is None
    assert not [event for event, _ in events if event == "mutation"]


async def test_disconnect_cancels_standalone_render_and_releases_assistant_lane(streaming_client, monkeypatch):
    from backend.api.routes.assistant import ASSISTANT_STREAM_KEY

    started, cleaned = asyncio.Event(), asyncio.Event()
    monkeypatch.setattr(images, "_images", OrderedDict())

    async def render(ctx):
        ctx.phase("Rendering…")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()
        return StandaloneResult(b"never", "image/png")

    with register_for_test(_workflow(render)):
        _script(monkeypatch, _call("create_image", {"description": "A portrait."}))
        async with streaming_client.stream("POST", RUN, json=_request()) as response:
            async for line in response.aiter_lines():
                if line == "event: start":
                    await asyncio.wait_for(started.wait(), 5)
                    active = deps._active_streams[ASSISTANT_STREAM_KEY]
                    break
        await asyncio.wait_for(cleaned.wait(), 5)
        await asyncio.wait_for(active.settled.wait(), 5)
        assert not images._images and ASSISTANT_STREAM_KEY not in deps._active_streams
        _script(monkeypatch, _reply("The assistant is ready."))
        events = frames(await streaming_client.post_checked(RUN, json=_request()))
        assert events[-1] == ("done", {"reason": "reply", "message": ""})


async def test_an_earlier_assistant_picture_can_be_an_explicit_reference_and_expiry_is_clear(client, db, monkeypatch):
    await build_library(client, db)
    source_id = images.keep_image(b"source picture", "image/png")
    seen = []

    async def render(ctx):
        seen.append(ctx.reference)
        return StandaloneResult(b"new picture", "image/png", "data:image/png;base64,eA==")

    with register_for_test(_workflow(render)):
        _script(
            monkeypatch,
            _call("create_image", {"description": "Same picture with a blue coat.", "reference_image_id": source_id}),
            _call("create_image", {"description": "Expired reference.", "reference_image_id": "f" * 32}),
            _reply("Done."),
        )
        from backend.inference import LLMClient

        complete = LLMClient.complete
        captured = []

        async def recording(self, **kwargs):
            captured.append(kwargs["messages"])
            async for event in complete(self, **kwargs):
                yield event

        monkeypatch.setattr(LLMClient, "complete", recording)
        events = frames(await client.post_checked(RUN, json=_request()))
    assert any(
        isinstance(message["content"], list) and message["content"][-1]["type"] == "image_url" for message in captured[1]
    ), "the next assistant step receives the generated preview"
    success, missing = _results(events)
    assert success["ok"] and not missing["ok"]
    assert "expired" in missing["summary"]
    assert len(seen) == 1 and seen[0].data == b"source picture" and seen[0].origin == f"assistant:{source_id}"
    assert "base64" not in str(events), "image bytes belong to the model conversation, not SSE or browser storage"
