"""The assistant's media tools against scripted workflow backends: dispatch, the enablement refusal, Stop and deletion."""

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import uuid
from datetime import UTC, datetime

from PIL import Image

from backend.api import deps
from backend.database import (
    get_active_path,
    get_message_by_id,
    get_workflow_attachments_for_message,
    insert_workflow_attachment_row,
    message_workflow_attachment_ids,
    set_workflow_enabled,
)
from backend.database.queries import messages as message_queries
from backend.inference import LLMClient
from backend.workflows import StandaloneResult, WorkflowEventStream, set_workflow_config
from backend.workflows.attachment_cache import insert_workflow_attachment, set_active_sibling
from backend.workflows.image_gen.engine import ImageResult
from tests.integration.test_assistant import RUN, _call, _reply, _script, build_library, frames
from tests.integration.workflows._fixtures import (  # noqa: F401
    _restore_registry,
    make_workflow,
    register_for_test,
)


def _body(chat):
    return {
        "text": "Make an image of the last reply.",
        "session": uuid.uuid4().hex,
        "context": {"mode": "chat", "conversation_id": chat},
    }


def _results(events):
    return [data for event, data in events if event == "tool_result"]


async def _save(message_id, workflow_id="image_gen"):
    new_id, _ = await insert_workflow_attachment(
        message_id, {"workflow_id": workflow_id, "filename": "out.png", "mime": "image/png", "data": b"PNG"}
    )
    return new_id


def _media(workflow_id, on_demand=None, **hooks):
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
        **hooks,
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
            seen.append(
                (
                    len(deps.work_registry.jobs(chat, job="assistant:op-1", message_ids={kept})),
                    len(deps.work_registry.jobs(chat)),
                )
            )
            yield {"event": "phase_status", "data": {"label": "Rendering in ComfyUI..."}}
            first = await _save(body["message_id"])
            yield {"event": "image_gen_render", "data": {"attachment_id": first}}
            # A review turn: the first render is in the chat before its redo is saved.
            await asyncio.sleep(0.05)
            redo = await _save(body["message_id"])
            yield {"event": "image_gen_render", "data": {"attachment_id": redo}}
            yield {"event": "image_gen_done", "data": {"attachment_id": redo}}

        return WorkflowEventStream(events=events())

    async def speech(ctx, body):
        seen.append("speech")
        return {"attachment_id": await _save(body["message_id"], "tts")}

    with register_for_test(_media("image_gen", on_demand)), register_for_test(_media("tts", speech)):
        await set_workflow_enabled("tts", False)
        _script(
            monkeypatch,
            _call("illustrate_reply", {"message_id": kept, "direction": "  From behind, at dusk. "}),
            _call("generate_speech", {"message_id": kept}),
            _reply("Done."),
        )
        events = frames(await client.post_checked(RUN, json=_body(chat)))

        # With every workflow switched off, the refusal names the master switch and the workflow's own.
        await client.put_checked("/api/settings", json={"workflows_globally_enabled": False})
        _script(monkeypatch, _call("generate_speech", {"message_id": kept}), _reply("No."))
        [both] = _results(frames(await client.post_checked(RUN.replace("op-1", "op-2"), json=_body(chat))))

    # The user's direction rides in the hook's own body, beside the reply it is made of.
    assert seen[0] == ({"action": "generate", "message_id": kept, "direction": "From behind, at dusk."}, library["card"])
    assert seen[1] == (1, 1)
    assert "speech" not in seen, "the switched-off workflow never reached its hook"
    saved = await message_workflow_attachment_ids(kept, "image_gen")
    assert len(saved) == 2
    assert ("progress", {"label": "Rendering in ComfyUI..."}) in events
    result, refused = _results(events)
    assert result["ok"] is True and result["ids"]["attachment_ids"] == saved
    assert refused["ok"] is False and "`workflow:tts`" in refused["summary"] and "set_pipeline_toggle" in refused["summary"]
    assert "`workflows`" not in refused["summary"]
    assert "`workflows`" in both["summary"] and "`workflow:tts`" in both["summary"]
    media = {"kind": "media", "conversation_id": chat, "message_id": kept, "workflow_id": "image_gen"}
    names = [event for event, _ in events]
    mutations = [data["resources"] for event, data in events if event == "mutation"]
    # Each render is announced as it is saved, before the tool settles; the settled write names them all.
    assert mutations[0] == [{**media, "attachment_ids": saved[:1]}]
    assert mutations[-1] == [{**media, "attachment_ids": saved}]
    assert names.index("mutation") < names.index("tool_result")
    assert events[-1] == ("done", {"reason": "reply", "message": ""})
    assert not deps.work_registry.jobs(chat), "the job settled with the stream"
    assert await message_workflow_attachment_ids(kept, "tts") == []


async def test_a_failed_hook_is_a_tool_error_and_a_workflow_without_an_on_demand_hook_offers_no_tool(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]

    async def speech(ctx, body):
        return {"error": "synthesis failed"}

    with register_for_test(_media("image_gen")), register_for_test(_media("tts", speech)):
        names = []
        script = [_call("generate_speech", {"message_id": kept}), _reply("It failed.")]

        async def complete(self, **kwargs):
            names.extend(tool["function"]["name"] for tool in kwargs["tools"])
            yield script.pop(0)

        monkeypatch.setattr("backend.inference.LLMClient.complete", complete)
        events = frames(await client.post_checked(RUN, json=_body(chat)))
    [failed] = _results(events)
    assert failed["ok"] is False and failed["summary"] == "No speech was made: synthesis failed"
    assert not [event for event, _ in events if event == "mutation"]
    assert "illustrate_reply" not in names and "generate_speech" in names


async def test_assistant_stop_cancels_its_own_render_alone_and_chat_stop_leaves_it(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    started, withdrawn = asyncio.Event(), []
    elsewhere = deps.start_workflow_job(chat, asyncio.Event().wait(), job="the-image-button", message_id=kept)
    with register_for_test(_blocking_image(started, withdrawn)):
        _script(monkeypatch, _call("illustrate_reply", {"message_id": kept}))
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
            _call("illustrate_reply", {"message_id": kept}),
            _reply("It was deleted."),
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
        _script(monkeypatch, _call("illustrate_reply", {"message_id": kept}))
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


async def test_both_image_tools_render_in_a_listed_style_and_an_unknown_one_is_refused_naming_the_styles(
    client, db, monkeypatch
):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    seen = []

    async def query(ctx, body):
        assert body == {"action": "styles"}
        return {
            "default_style": "realistic",
            "styles": [{"id": "realistic", "label": "Realistic"}, {"id": "ink", "label": "Ink"}],
        }

    async def on_demand(ctx, body):
        seen.append(dict(body))
        return {"attachment_id": await _save(body["message_id"])}

    async def standalone(ctx):
        seen.append(ctx.style_id)
        return StandaloneResult(b"image", "image/png")

    calls = []
    script = [
        _call("list_image_styles", {}),
        _call("illustrate_reply", {"message_id": kept, "style_id": "ink"}),
        _call("create_image", {"description": "A lighthouse.", "style_id": "ink"}),
        _call("create_image", {"description": "A lighthouse."}),
        _call("create_image", {"description": "A lighthouse.", "style_id": "Ink"}),
        _reply("Done."),
    ]

    async def complete(self, **kwargs):
        calls.append(kwargs["messages"])
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)
    with register_for_test(_media("image_gen", on_demand, standalone=standalone, query=query)):
        listed, illustrated, created, default, unknown = _results(frames(await client.post_checked(RUN, json=_body(chat))))

    listing = json.loads(next(message["content"] for message in calls[1] if message["role"] == "tool"))
    assert listing == {
        "default_style": "realistic",
        "styles": {"realistic": "Realistic", "ink": "Ink"},
        "takes_reference": [],
    }
    assert listed["ok"] and "Realistic (realistic)" in listed["summary"]
    assert seen == [{"action": "generate", "message_id": kept, "style_id": "ink"}, "ink", ""]
    assert illustrated["ok"] and created["ok"] and default["ok"]
    assert not unknown["ok"] and "ink (Ink)" in unknown["summary"]


def _png(color) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, "PNG")
    return buffer.getvalue()


async def _image(message_id, data, *, parent=None):
    return await insert_workflow_attachment_row(
        message_id,
        {
            "workflow_id": "image_gen",
            "filename": "x.png",
            "mime": "image/png",
            "data": data,
            "seed": "1",
            "parent_attachment_id": parent,
            "generation_metadata": {"style_id": "anime", "prompt": "1girl", "negative_prompt": ""},
        },
    )


async def _upload(db, message_id, data):
    cursor = await db.execute(
        "INSERT INTO user_attachments (message_id, mime_type, data_b64, filename, size, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (message_id, "image/png", base64.b64encode(data).decode(), "up.png", len(data), datetime.now(UTC).isoformat()),
    )
    await db.commit()
    return cursor.lastrowid


def _styles(**takes):
    async def query(ctx, body):
        assert body == {"action": "styles"}
        return {
            "default_style": next(iter(takes)),
            "styles": [{"id": sid, "label": sid.title(), "takes_reference": flag} for sid, flag in takes.items()],
        }

    return query


async def test_a_chosen_chat_image_is_the_reply_render_reference_and_a_reroll_replays_it(client, db, monkeypatch):
    from tests.integration.workflows.image_gen.test_hooks import CLOUD_CONFIG

    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    await set_workflow_config("image_gen", CLOUD_CONFIG)
    path = await get_active_path(chat)
    chosen_bytes = _png("blue")
    chosen = await _image(path[-6]["id"], chosen_bytes)
    # Newer, so the chat's own previous image: the chosen one must win over it.
    await _image(path[-3]["id"], _png("red"))
    renders, composed = [], []

    async def compose(**kwargs):
        composed.append(kwargs)
        return "1girl, standing", "", "single_call"

    async def render(adapter, request, **kwargs):
        renders.append(request)
        records = [reference.record() for reference in request.references]
        return ImageResult(_png("white"), "image/png", {"source": "cloud", "references": records})

    monkeypatch.setattr("backend.workflows.image_gen.hooks.compose_scene", compose)
    monkeypatch.setattr("backend.workflows.image_gen.hooks.resolve_and_generate", render)
    _script(
        monkeypatch,
        _call("list_image_styles", {}),
        _call("illustrate_reply", {"message_id": kept, "reference_image": f"attachment:{chosen}"}),
        _reply("Done."),
    )
    listed, illustrated = _results(frames(await client.post_checked(RUN, json=_body(chat))))

    assert listed["ok"] and illustrated["ok"], illustrated
    # The chosen image is edited, not redrawn: the prompter states the change and knows what the image was drawn from.
    assert composed[0]["edit"] is True and composed[0]["shows"] == "1girl"
    [sent] = renders[0].references
    assert sent.origin == f"attachment:{chosen}" and sent.source_digest == hashlib.sha256(chosen_bytes).hexdigest()
    [row] = await get_workflow_attachments_for_message(kept)
    metadata = json.loads(row["generation_metadata"])
    assert [entry["origin"] for entry in metadata["references"]] == [f"attachment:{chosen}"]
    assert metadata["reference"] == f"attachment:{chosen}"

    await client.post_checked(f"/api/conversations/{chat}/messages/{kept}/workflow-attachments/{row['id']}/reroll-gen", json={})
    assert [reference.origin for reference in renders[1].references] == [f"attachment:{chosen}"]


async def test_a_reference_for_a_style_without_image_inputs_is_refused_naming_the_styles_that_take_one(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    chosen = await _image((await get_active_path(chat))[-3]["id"], _png("blue"))
    made = []

    async def on_demand(ctx, body):
        made.append(body)
        return {"attachment_id": await _save(body["message_id"])}

    async def standalone(ctx):
        made.append(ctx)
        return StandaloneResult(b"image", "image/png")

    with register_for_test(_media("image_gen", on_demand, standalone=standalone, query=_styles(plain=False, edit=True))):
        _script(
            monkeypatch,
            _call("illustrate_reply", {"message_id": kept, "reference_image": f"attachment:{chosen}"}),
            _call("create_image", {"description": "A lighthouse.", "reference_image": f"attachment:{chosen}"}),
            _reply("Neither style works."),
        )
        illustrated, created = _results(frames(await client.post_checked(RUN, json=_body(chat))))

    assert not made
    for result in (illustrated, created):
        assert not result["ok"] and "edit (Edit)" in result["summary"] and "plain (Plain)" not in result["summary"]


async def test_create_image_takes_chat_images_and_uploads_and_a_reply_refuses_a_panel_image(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    path = await get_active_path(chat)
    rendered, uploaded = _png("blue"), _png("green")
    render_id = await _image(path[-3]["id"], rendered)
    upload_id = await _upload(db, path[-2]["id"], uploaded)
    seen = []

    async def on_demand(ctx, body):
        seen.append(body)
        return {"attachment_id": await _save(body["message_id"])}

    async def standalone(ctx):
        seen.append(ctx.reference)
        return StandaloneResult(b"image", "image/png")

    with register_for_test(_media("image_gen", on_demand, standalone=standalone, query=_styles(edit=True))):
        _script(
            monkeypatch,
            _call("illustrate_reply", {"message_id": kept, "reference_image": "image:" + "a" * 32}),
            _call("create_image", {"description": "The same, at night.", "reference_image": f"attachment:{render_id}"}),
            _call("create_image", {"description": "The same, at night.", "reference_image": f"upload:{upload_id}"}),
            _reply("Done."),
        )
        refused, from_render, from_upload = _results(frames(await client.post_checked(RUN, json=_body(chat))))

    assert not refused["ok"] and from_render["ok"] and from_upload["ok"]
    # The image workflow reads a chat image's bytes itself, by origin.
    assert [(reference.origin, reference.data) for reference in seen] == [
        (f"attachment:{render_id}", b""),
        (f"upload:{path[-2]['id']}:{upload_id}", b""),
    ]


async def test_read_messages_lists_each_message_images_with_the_one_on_screen_and_reads_no_bytes(client, db, monkeypatch):
    library = await build_library(client, db)
    chat, kept = library["chat"], library["kept"]
    asked = (await get_active_path(chat))[-2]["id"]
    root = await _image(kept, _png("blue"))
    redo = await _image(kept, _png("red"), parent=root)
    await set_active_sibling(root, root, expected_message_id=kept)
    upload = await _upload(db, asked, _png("green"))
    queries, calls = [], []
    select_rows = message_queries.select_rows

    async def recording(sql, *args, **kwargs):
        queries.append(sql)
        return await select_rows(sql, *args, **kwargs)

    monkeypatch.setattr(message_queries, "select_rows", recording)
    script = [_call("read_messages", {"count": 2}), _reply("Two images.")]

    async def complete(self, **kwargs):
        calls.append(kwargs["messages"])
        yield script.pop(0)

    monkeypatch.setattr(LLMClient, "complete", complete)
    await client.post_checked(RUN, json=_body(chat))

    result = json.loads(next(message["content"] for message in calls[1] if message["role"] == "tool"))
    rows = {row["id"]: row for row in result["messages"]}
    assert rows[kept]["images"] == [
        {"ref": f"attachment:{redo}", "kind": "render", "shown": False},
        {"ref": f"attachment:{root}", "kind": "render", "shown": True},
    ]
    assert rows[asked]["images"] == [{"ref": f"upload:{upload}", "kind": "upload", "shown": True}]
    assert queries and not [sql for sql in queries if "data_b64" in sql]
