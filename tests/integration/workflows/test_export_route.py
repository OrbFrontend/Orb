"""The export route serves what a workflow's EXPORT hook returns, under a filename and note header safe to send, and loads the
stored bytes only when the hook asks for them."""

from __future__ import annotations

from backend.database import add_message, insert_workflow_attachment_row, set_active_leaf
from backend.workflows import ExportedFile, HookType, subscribe

from ._fixtures import make_workflow, register_for_test


async def _no_variants(ctx, body):
    return []


async def _no_reroll(ctx, params, seed):
    return b""


async def _artifact(client, workflow_id: str) -> int:
    cid = await client.create("/api/conversations", json={"title": "export"})
    mid, _ = await add_message(cid, "assistant", "a scene", 0)
    await set_active_leaf(cid, mid)
    return await insert_workflow_attachment_row(
        mid, {"filename": "render.webp", "mime": "image/webp", "data": b"stored-copy", "workflow_id": workflow_id}
    )


async def test_export_serves_the_hook_file_with_safe_headers(client):
    seen: list[bytes | None] = []

    async def export(ctx):
        seen.append(await ctx.stored_bytes())
        return ExportedFile(
            data=b"full-png", mime="image/png", filename='../a "b".png', note="Converted from\nthe stored copy — sorry."
        )

    workflow = make_workflow("exp", produces_artifacts=True, regenerate=_no_variants, reroll_gen=_no_reroll)
    with register_for_test(workflow):
        subscribe("exp", HookType.EXPORT, export)
        aid = await _artifact(client, "exp")
        resp = await client.get(f"/api/workflow-attachments/{aid}/export")

    assert resp.status_code == 200
    assert resp.content == b"full-png"
    assert seen == [b"stored-copy"]
    assert resp.headers["content-type"] == "image/png"
    assert resp.headers["content-disposition"] == 'attachment; filename="a_b_.png"'
    assert resp.headers["x-orb-export-note"] == "Converted from the stored copy ? sorry."


async def test_export_answers_gone_when_nothing_is_left_and_not_found_without_a_hook(client):
    async def export(ctx):
        return None

    workflow = make_workflow("exp", produces_artifacts=True, regenerate=_no_variants, reroll_gen=_no_reroll)
    with register_for_test(workflow):
        subscribe("exp", HookType.EXPORT, export)
        gone = await client.get(f"/api/workflow-attachments/{await _artifact(client, 'exp')}/export")
    assert gone.status_code == 410

    no_hook = await client.get(f"/api/workflow-attachments/{await _artifact(client, 'tts')}/export")
    assert no_hook.status_code == 404
