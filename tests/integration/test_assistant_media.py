"""The assistant's media tools against scripted workflow backends: dispatch, the enablement refusal, Stop and deletion."""

import asyncio
import contextlib

from backend.api import deps
from backend.database import get_message_by_id, message_workflow_attachment_ids, set_workflow_enabled
from backend.workflows import WorkflowEventStream
from backend.workflows.attachment_cache import insert_workflow_attachment
from tests.integration.test_assistant import RUN, _call, _script, _step, build_library, frames
from tests.integration.workflows._fixtures import (  # noqa: F401
    _restore_registry,
    make_workflow,
    register_for_test,
)


def _body(chat):
    return {
        "items": [{"kind": "user", "text": "Make an image of the last reply."}],
        "context": {"mode": "chat", "conversation_id": chat},
    }


def _results(events):
    return [data for event, data in events if event == "tool_result"]


async def _save(message_id, workflow_id="image_gen"):
    new_id, _ = await insert_workflow_attachment(
        message_id, {"workflow_id": workflow_id, "filename": "out.png", "mime": "image/png", "data": b"PNG"}
    )
    return new_id


def _media(workflow_id, on_demand=None):
    """A scripted artifact workflow under a real workflow's id, standing in for its renderer."""

    async def regenerate(ctx, body):
        return []

    async def reroll_gen(ctx, params, seed):
        return b""

    name = {"image_gen": "Image Generation", "tts": "Text-to-Speech"}[workflow_id]
    return make_workflow(
        workflow_id,
        display_name=name,
        on_demand=on_demand,
        regenerate=regenerate,
        reroll_gen=reroll_gen,
        produces_artifacts=True,
    )


def _blocking_image(started, withdrawn):
    """An image hook whose render waits until it is cancelled, then withdraws its queued job, as ComfyUI's does."""

    async def on_demand(ctx, body):
        async def events():
            yield {"event": "phase_status", "data": {"label": "Queued on ComfyUI..."}}
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                withdrawn.append(body["message_id"])
            yield {"event": "image_gen_done", "data": {"attachment_id": None}}

        return WorkflowEventStream(events=events())

    return _media("image_gen", on_demand)


async def test_a_render_runs_inside_one_job_and_a_switched_off_workflow_refuses_naming_its_switch(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    seen = []

    async def on_demand(ctx, body):
        seen.append((dict(body), ctx.character_id))

        async def events():
            # Consumed lazily, after the dispatch has returned: still inside one job filed under the run and the message.
            jobs = deps._workflow_jobs.get(chat, {})
            seen.append(sorted((name, deps._workflow_sources[task]) for task, name in jobs.items()))
            yield {"event": "phase_status", "data": {"label": "Rendering in ComfyUI..."}}
            first = await _save(body["message_id"])
            yield {"event": "image_gen_render", "data": {"attachment_id": first}}
            yield {"event": "image_gen_done", "data": {"attachment_id": first}}

        return WorkflowEventStream(events=events())

    async def speech(ctx, body):
        seen.append("speech")
        return {"attachment_id": await _save(body["message_id"], "tts")}

    with register_for_test(_media("image_gen", on_demand)), register_for_test(_media("tts", speech)):
        await set_workflow_enabled("tts", False)
        _script(
            monkeypatch,
            _step("generate_image"),
            _call("generate_image", {"message_id": kept}),
            _step("generate_speech"),
            _call("generate_speech", {"message_id": kept}),
            _step("reply", "Done."),
        )
        events = frames(await client.post_checked(RUN, json=_body(chat)))

        # With every workflow switched off, the refusal names the master switch and the workflow's own.
        await client.put_checked("/api/settings", json={"workflows_globally_enabled": False})
        _script(monkeypatch, _step("generate_speech"), _call("generate_speech", {"message_id": kept}), _step("reply", "No."))
        [both] = _results(frames(await client.post_checked(RUN.replace("op-1", "op-2"), json=_body(chat))))

    assert seen[0] == ({"action": "generate", "message_id": kept}, library["card"])
    assert seen[1] == [("assistant:op-1", kept)]
    assert "speech" not in seen, "the switched-off workflow never reached its hook"
    saved = await message_workflow_attachment_ids(kept, "image_gen")
    assert len(saved) == 1
    assert ("progress", {"label": "Rendering in ComfyUI..."}) in events
    result, refused = _results(events)
    assert result["ok"] is True and result["ids"]["attachment_ids"] == saved
    assert refused["ok"] is False and "`workflow:tts`" in refused["summary"] and "set_pipeline_toggle" in refused["summary"]
    assert "`workflows`" not in refused["summary"]
    assert "`workflows`" in both["summary"] and "`workflow:tts`" in both["summary"]
    mutation = next(data for event, data in events if event == "mutation")
    assert mutation["resources"] == [
        {"kind": "media", "conversation_id": chat, "message_id": kept, "attachment_ids": saved, "workflow_id": "image_gen"}
    ]
    assert events[-1] == ("done", {"reason": "reply", "message": ""})
    assert not deps._workflow_jobs.get(chat), "the job settled with the stream"
    assert await message_workflow_attachment_ids(kept, "tts") == []


async def test_a_failed_hook_is_a_tool_error_and_a_workflow_without_an_on_demand_hook_offers_no_tool(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]

    async def speech(ctx, body):
        return {"error": "synthesis failed"}

    with register_for_test(_media("image_gen")), register_for_test(_media("tts", speech)):
        names = []
        script = [_step("generate_speech"), _call("generate_speech", {"message_id": kept}), _step("reply", "It failed.")]

        async def complete(self, **kwargs):
            names.extend(kwargs["tools"][0]["function"]["parameters"]["properties"]["tool"]["enum"])
            yield script.pop(0)

        monkeypatch.setattr("backend.inference.LLMClient.complete", complete)
        events = frames(await client.post_checked(RUN, json=_body(chat)))
    [failed] = _results(events)
    assert failed["ok"] is False and failed["summary"] == "No speech was made: synthesis failed"
    assert not [event for event, _ in events if event == "mutation"]
    assert "generate_image" not in names and "generate_speech" in names


async def test_assistant_stop_cancels_its_own_render_alone_and_chat_stop_leaves_it(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    started, withdrawn = asyncio.Event(), []
    elsewhere = deps.start_workflow_job(chat, asyncio.Event().wait(), job="the-image-button", message_id=kept)
    with register_for_test(_blocking_image(started, withdrawn)):
        _script(monkeypatch, _step("generate_image"), _call("generate_image", {"message_id": kept}))
        run = asyncio.create_task(client.post(RUN, json=_body(chat)))
        await asyncio.wait_for(started.wait(), 5)
        try:
            # The chat's own Stop is for the chat's reply: a render beside it carries on.
            assert (await client.post_json(f"/api/conversations/{chat}/stop"))["active"] is False
            assert withdrawn == [] and not run.done()
            stop = await client.post_json("/api/assistant/stop?operation_id=op-1")
            assert stop == {"ok": True, "active": True, "settled": True}
            events = frames(await asyncio.wait_for(run, 5))
            assert withdrawn == [kept], "the render was cancelled and withdrew its job"
            assert not elsewhere.done(), "a render the user started on the same message runs on"
        finally:
            elsewhere.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await elsewhere
    [result] = _results(events)
    assert result["ok"] is False and result["summary"].startswith("Stopped before any image was saved")
    assert events[-1] == ("done", {"reason": "stopped", "message": ""})


async def test_deleting_the_message_mid_render_settles_the_job_and_the_tool_reports_it(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    started, withdrawn = asyncio.Event(), []
    with register_for_test(_blocking_image(started, withdrawn)):
        _script(
            monkeypatch,
            _step("generate_image"),
            _call("generate_image", {"message_id": kept}),
            _step("reply", "It was deleted."),
        )
        run = asyncio.create_task(client.post(RUN, json=_body(chat)))
        await asyncio.wait_for(started.wait(), 5)
        # The delete finds the job by the message it renders, settles it, then deletes.
        assert (await client.delete(f"/api/conversations/{chat}/messages/{kept}")).status_code == 200
        assert withdrawn == [kept] and await get_message_by_id(kept) is None
        events = frames(await asyncio.wait_for(run, 5))
    [result] = _results(events)
    assert result["ok"] is False and "was deleted while its image was being made" in result["summary"]
    assert events[-1] == ("done", {"reason": "reply", "message": ""})


async def test_a_stopped_render_never_claims_an_image_another_render_saved(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    started, withdrawn = asyncio.Event(), []
    with register_for_test(_blocking_image(started, withdrawn)):
        _script(monkeypatch, _step("generate_image"), _call("generate_image", {"message_id": kept}))
        run = asyncio.create_task(client.post(RUN, json=_body(chat)))
        await asyncio.wait_for(started.wait(), 5)
        # The user's own render from the image button saves on the same message while the assistant's is still queued.
        theirs = await _save(kept)
        stop = await client.post_json("/api/assistant/stop?operation_id=op-1")
        assert stop["settled"] is True
        events = frames(await asyncio.wait_for(run, 5))
    [result] = _results(events)
    assert result["ok"] is False and result["ids"] == {}
    assert "may have been saved" in result["summary"] and "check the message" in result["summary"]
    assert not [event for event, _ in events if event == "mutation"], "the other render's image is not this run's"
    assert events[-1] == ("done", {"reason": "stopped", "message": ""}), "nothing is listed as already done"
    assert await message_workflow_attachment_ids(kept, "image_gen") == [theirs]
