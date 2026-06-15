"""Secondary-workflow routes: manifest, config, on-demand trigger, and the
workflow-attachment lifecycle (regenerate / reroll-gen / rehydrate / activate /
delete / access)."""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping

from fastapi import APIRouter, Body, HTTPException

from ...core import (
    scrub_log,
    workflow_character_state_lock,
    workflow_config_lock,
    workflow_state_lock,
)
from ...database import (
    get_character_card,
    get_conversation,
    get_db,
    get_message_by_id,
    get_messages,
    get_messages_before,
    get_settings,
    get_workflow_attachment_by_id,
)
from ...inference import LLMClient
from ...workflows import (
    HookType,
    OnDemandCtx,
    RegenCtx,
    RerollGenCtx,
    _readonly,
    get_subscription,
    get_workflow,
    get_workflow_config,
    list_workflows,
    set_workflow_config,
)
from ...workflows.attachment_cache import (
    EVICTED_MARKER,
    OVERSIZE_NO_METADATA_REASON,
    RehydrateAlreadyDoneError,
    delete_workflow_attachments,
    insert_workflow_attachment,
    insert_workflow_attachments,
    record_access,
    rehydrate_attachment,
    set_active_sibling,
    validate_workflow_attachment_shape,
)
from ..deps import _workflow_root_lock
from ..schemas import WorkflowConfigUpdate

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/api/workflows")
async def api_list_workflows():
    """Manifest the frontend reads once at boot to populate Secondary tabs and buttons."""
    return [
        {
            "id": w.id,
            "display_name": w.display_name,
            "config_schema": w.config_schema,
            "config_defaults": w.config_defaults,
        }
        for w in list_workflows()
    ]


@router.put("/api/workflows/{workflow_id}/config")
async def api_set_workflow_config(workflow_id: str, data: WorkflowConfigUpdate):
    """Persist a workflow's global config slot as a full replacement."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    # Serialize the replacement with workflow code that updates the same slot via
    # a locked read-modify-write; a lock-free write here could be lost mid-RMW.
    async with workflow_config_lock():
        await set_workflow_config(workflow_id, data.config)
        effective = await get_workflow_config(workflow_id)
    logger.info("workflow %r config updated (%d keys)", scrub_log(workflow_id), len(data.config))
    return {"config": effective}


@router.get("/api/workflows/{workflow_id}/config")
async def api_get_workflow_config(workflow_id: str):
    """Return a workflow's effective config: persisted slot, else its defaults."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    return {"config": await get_workflow_config(workflow_id)}


@router.post("/api/conversations/{cid}/workflows/{workflow_id}/trigger")
async def api_trigger_workflow(cid: str, workflow_id: str, body: dict = Body(default={})):  # noqa: B008
    """Run a workflow's on_demand hook against the current conversation state."""
    if get_workflow(workflow_id) is None:
        raise HTTPException(status_code=404, detail=f"Workflow {workflow_id!r} is not registered")
    sub = get_subscription(workflow_id, HookType.ON_DEMAND)
    if sub is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow {workflow_id!r} has no on_demand handler",
        )
    # Serialize against the pre/post hook iteration of an in-flight pipeline and
    # against any other /trigger for the same (cid, workflow_id), so the prior
    # workflow_state read the hook depends on cannot be clobbered between read
    # and write by a concurrent caller.
    async with workflow_state_lock(cid, workflow_id):
        conv = await get_conversation(cid)
        if conv is None:
            raise HTTPException(status_code=404, detail="Conversation not found")
        card_id = conv.get("character_card_id")
        card = await get_character_card(card_id) if card_id else None
        msgs = await get_messages(cid)
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
        )
        async with workflow_character_state_lock(conv.get("character_card_id") or "", workflow_id):
            try:
                od_ctx = OnDemandCtx(
                    conversation_id=cid,
                    history=_readonly(msgs),
                    last_user_message=last_user,
                    settings=_readonly(settings_snapshot),
                    client=client,
                    character_id=conv.get("character_card_id"),
                    character=_readonly(card),
                )
                return await sub.callable(od_ctx, body)
            except Exception:
                logger.exception("on_demand hook %r failed", scrub_log(workflow_id))
                raise HTTPException(status_code=500, detail="On-demand handler raised; see server logs") from None


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/regenerate")
async def api_regenerate_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008
    """Append a new sibling variant under a workflow-produced attachment's root."""
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    wid = att.get("workflow_id")
    sub = get_subscription(wid, HookType.REGENERATE) if wid else None
    if sub is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow {wid!r} is not registered or has no regenerate handler",
        )
    # Single hop suffices: the dispatcher itself assigns parent_attachment_id = root_id
    # on every write, so the variant tree is flat by construction (root + N siblings).
    root_id = att["parent_attachment_id"] or aid

    async with _workflow_root_lock(root_id):
        anchor = await get_message_by_id(mid)
        if anchor is None or anchor["conversation_id"] != cid:
            raise HTTPException(status_code=404, detail="Message not found in conversation")
        msgs = await get_messages_before(cid, mid)
        last_user = next((m["content"] for m in reversed(msgs) if m["role"] == "user"), "")
        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
        )

        card_id = conv.get("character_card_id")
        card = await get_character_card(card_id) if card_id else None
        try:
            regen_ctx = RegenCtx(
                conversation_id=cid,
                message_id=mid,
                attachment_id=aid,
                original_attachment=_readonly(att),
                history=_readonly(msgs),
                last_user_message=last_user,
                settings=_readonly(settings_snapshot),
                client=client,
                character_id=conv.get("character_card_id"),
                character=_readonly(card),
            )
            new_dicts = await sub.callable(regen_ctx, body)
        except Exception:
            logger.exception("regenerate hook %r failed for attachment %r", scrub_log(wid), scrub_log(aid))
            raise HTTPException(status_code=500, detail="Regenerate handler raised; see server logs") from None

        if not isinstance(new_dicts, list):
            logger.warning(
                "regenerate hook %r returned non-list (%s); treating as empty",
                wid,
                type(new_dicts).__name__,
            )
            new_dicts = []

        # Bad-shape entries are partitioned to rejected_workflow_atts so a
        # single bad entry does not roll back the batch insert. Non-dict
        # entries are dropped instead of rejected because the rejection
        # record requires a filename to surface in the UI.
        fixed: list[dict] = []
        rejected_pre: list[dict] = []
        for d in new_dicts:
            if not isinstance(d, dict):
                logger.warning("regenerate hook %r returned non-dict entry; skipping", wid)
                continue
            candidate = {**d, "workflow_id": sub.workflow_id, "parent_attachment_id": root_id}
            ok, reason = validate_workflow_attachment_shape(candidate)
            if not ok:
                rejected_pre.append(
                    {
                        "filename": candidate.get("filename") if isinstance(candidate.get("filename"), str) else None,
                        "workflow_id": sub.workflow_id,
                        "mime": candidate.get("mime") if isinstance(candidate.get("mime"), str) else None,
                        "reason": reason,
                        "originating_attachment_id": root_id,
                    }
                )
                logger.info(
                    "regenerate hook %r returned attachment rejected by shape validator: %s",
                    wid,
                    reason,
                )
                continue
            fixed.append(candidate)

        if not fixed and not rejected_pre:
            return {"attachments": [], "rejected_workflow_atts": []}

        try:
            new_ids, helper_rejected = await insert_workflow_attachments(mid, fixed)
        except (ValueError, LookupError, OSError):
            logger.exception("regenerate hook %r batch insert failed", wid)
            raise HTTPException(status_code=500, detail="Regenerate batch insert failed; see server logs") from None

        helper_rejected_projected = [
            {
                "filename": a.get("filename"),
                "workflow_id": a.get("workflow_id"),
                "mime": a.get("mime"),
                "reason": a.get("reason") or OVERSIZE_NO_METADATA_REASON,
                "originating_attachment_id": root_id,
            }
            for a in helper_rejected
        ]
        return {
            "attachments": new_ids,
            "rejected_workflow_atts": rejected_pre + helper_rejected_projected,
        }


def _decode_stored_consumption_metadata(att: Mapping[str, Any]) -> dict | None:
    """Parse the parent attachment's stored consumption_metadata JSON.

    Returns the decoded dict, or ``None`` for any malformed or non-dict value.
    """
    raw = att.get("consumption_metadata")
    if not raw:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _split_reroll_gen_result(result, workflow_id: str | None) -> tuple[object, dict | None]:
    """Split a reroll_gen hook return into ``(data, consumption_metadata)``.

    A raw ``bytes`` return carries no metadata; a ``(bytes, dict | None)``
    tuple supplies a fresh ``consumption_metadata``. A non-dict second element
    is dropped with a warning. The caller validates that ``data`` is non-empty
    bytes. Shared by the reroll-gen and rehydrate routes so both interpret the
    hook return identically.
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (bytes, bytearray)):
        data, consumption_metadata = result
        if consumption_metadata is not None and not isinstance(consumption_metadata, dict):
            logger.warning(
                "reroll_gen hook %r returned tuple with non-dict consumption_metadata (%s); coercing to None",
                workflow_id,
                type(consumption_metadata).__name__,
            )
            consumption_metadata = None
        return data, consumption_metadata
    return result, None


def _build_reroll_gen_ctx(
    cid: str, mid: int, aid: int, att: Mapping[str, Any], settings: Mapping[str, Any], client
) -> RerollGenCtx:
    prior_cm = _decode_stored_consumption_metadata(att)
    return RerollGenCtx(
        conversation_id=cid,
        message_id=mid,
        attachment_id=aid,
        original_attachment=_readonly(att),
        settings=_readonly(settings),
        client=client,
        prior_consumption_metadata=_readonly(prior_cm) if prior_cm is not None else None,
    )


def _generated_seed() -> str:
    import secrets

    return secrets.token_hex(16)


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/reroll-gen")
async def api_reroll_gen_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008, ARG001
    """Generate a new sibling using the original's stored generation_metadata
    with a freshly minted seed.

    The new sibling persists the new seed alongside the inherited
    generation_metadata so it is itself rehydratable; without that, an
    evict-then-rehydrate cycle would lose the rerolled output.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    wid = att.get("workflow_id")
    sub = get_subscription(wid, HookType.REROLL_GEN) if wid else None
    if sub is None:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow {wid!r} is not registered or has no reroll_gen handler",
        )

    metadata_raw = att.get("generation_metadata")
    try:
        params = json.loads(metadata_raw) if metadata_raw else {}
    except (TypeError, ValueError):
        params = {}
    if not isinstance(params, dict):
        params = {}

    root_id = att["parent_attachment_id"] or aid

    async with _workflow_root_lock(root_id):
        seed = _generated_seed()
        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
        )

        try:
            ctx = _build_reroll_gen_ctx(cid, mid, aid, att, settings_snapshot, client)
            result = await sub.callable(ctx, params, seed)
        except Exception:
            logger.exception("reroll_gen hook %r failed for attachment %r", scrub_log(wid), scrub_log(aid))
            raise HTTPException(status_code=500, detail="reroll_gen handler raised; see server logs") from None

        data, new_consumption_metadata = _split_reroll_gen_result(result, wid)

        if not isinstance(data, (bytes, bytearray)) or not data:
            raise HTTPException(status_code=500, detail="reroll_gen handler returned no bytes")

        new_attachment = {
            "workflow_id": sub.workflow_id,
            "parent_attachment_id": root_id,
            "filename": att.get("filename") or sub.workflow_id,
            "mime": att.get("mime_type") or "application/octet-stream",
            "data": bytes(data),
            "seed": seed,
            "generation_metadata": params,
            "consumption_metadata": new_consumption_metadata,
            "annotation": att.get("annotation"),
        }
        try:
            new_id, rejected = await insert_workflow_attachment(mid, new_attachment)
        except (ValueError, LookupError, OSError):
            logger.exception("reroll_gen hook %r yielded an attachment that failed insert", wid)
            raise HTTPException(status_code=500, detail="reroll_gen insert failed; see server logs") from None

        return {
            "attachment_id": new_id,
            "rejected_workflow_atts": (
                [
                    {
                        "filename": rejected.get("filename"),
                        "workflow_id": rejected.get("workflow_id"),
                        "mime": rejected.get("mime"),
                        "reason": rejected.get("reason") or OVERSIZE_NO_METADATA_REASON,
                        "originating_attachment_id": root_id,
                    }
                ]
                if rejected is not None
                else []
            ),
        }


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/rehydrate")
async def api_rehydrate_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008, ARG001
    """Recover bytes for an evicted attachment using its stored seed + params.

    Preconditions:
      - The row's `data_b64` is the EVICTED_MARKER sentinel.
      - The row has a non-NULL `seed`.

    The framework calls the workflow's `reroll_gen` hook with the stored
    params and stored seed, then writes the returned bytes back into the
    same row's data_b64. No new sibling is created.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    if att.get("data_b64") != EVICTED_MARKER:
        raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate")
    seed = att.get("seed")
    if not seed:
        raise HTTPException(status_code=409, detail="Attachment has no stored seed; cannot rehydrate")

    # Serialize same-root rehydrates the way /regenerate, /reroll-gen, and
    # /activate already do for their sibling-tree mutations. Without this,
    # two concurrent callers would each run the full reroll_gen LLM call
    # before the cache helper's transactional recheck deduplicates them at
    # the DB layer -- doubling LLM cost even though the row stays consistent.
    # parent_attachment_id is NULL on root rows, so `or aid` resolves to the
    # root id whether the request targets a sibling or the root itself.
    root_id = att["parent_attachment_id"] or aid
    async with _workflow_root_lock(root_id):
        # Re-read inside the lock so a concurrent caller that already
        # rehydrated cannot slip past the snapshot check above and double
        # the reroll_gen LLM call before the cache helper's transactional
        # recheck deduplicates the bytes write.
        att = await get_workflow_attachment_by_id(aid)
        if att is None or att.get("data_b64") != EVICTED_MARKER:
            raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate")
        wid = att.get("workflow_id")
        sub = get_subscription(wid, HookType.REROLL_GEN) if wid else None
        if sub is None:
            raise HTTPException(
                status_code=404,
                detail=f"Workflow {wid!r} is not registered or has no reroll_gen handler",
            )

        metadata_raw = att.get("generation_metadata")
        try:
            params = json.loads(metadata_raw) if metadata_raw else {}
        except (TypeError, ValueError):
            params = {}
        if not isinstance(params, dict):
            params = {}

        settings_snapshot = await get_settings()
        client = LLMClient(
            settings_snapshot["endpoint_url"],
            api_key=settings_snapshot.get("api_key", ""),
        )

        try:
            ctx = _build_reroll_gen_ctx(cid, mid, aid, att, settings_snapshot, client)
            result = await sub.callable(ctx, params, seed)
        except Exception:
            logger.exception("reroll_gen (rehydrate) %r failed for attachment %r", scrub_log(wid), scrub_log(aid))
            raise HTTPException(status_code=500, detail="reroll_gen handler raised; see server logs") from None

        data, new_consumption_metadata = _split_reroll_gen_result(result, wid)

        if not isinstance(data, (bytes, bytearray)) or not data:
            raise HTTPException(status_code=500, detail="reroll_gen handler returned no bytes")

        try:
            await rehydrate_attachment(aid, bytes(data), consumption_metadata=new_consumption_metadata)
        except RehydrateAlreadyDoneError:
            # Race with a concurrent rehydrate that already restored the bytes.
            # End state is correct; surface as 409 so the client treats it as
            # success rather than the generic 500.
            raise HTTPException(status_code=409, detail="Attachment bytes are present; nothing to rehydrate") from None
        except (LookupError, ValueError):
            logger.exception("rehydrate write failed for attachment %r", scrub_log(aid))
            raise HTTPException(status_code=500, detail="rehydrate write failed; see server logs") from None

        return {"attachment_id": aid}


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/activate")
async def api_activate_workflow_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008
    """Persist the user's active-sibling choice for a workflow attachment group.

    ``aid`` is the ROOT attachment id (``parent_attachment_id IS NULL``).
    Body shape: ``{"sibling_id": int | null}`` -- ``null`` clears the
    column, which reverts to "newest sibling wins" in the renderer.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")

    raw_sibling_id = body.get("sibling_id") if isinstance(body, dict) else None
    if raw_sibling_id is not None and (not isinstance(raw_sibling_id, int) or isinstance(raw_sibling_id, bool)):
        raise HTTPException(status_code=400, detail="sibling_id must be an integer or null")

    try:
        async with _workflow_root_lock(aid):
            await set_active_sibling(aid, raw_sibling_id, expected_message_id=mid)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"active_sibling_id": raw_sibling_id}


@router.post("/api/conversations/{cid}/messages/{mid}/workflow-attachments/{aid}/delete")
async def api_delete_workflow_attachment(cid: str, mid: int, aid: int, body: dict = Body(default={})):  # noqa: B008
    """Delete a workflow attachment: one variant, or the whole group.

    ``aid`` is the acted-on row. Body: ``{"scope": "variant" | "group"}``.
    Deleting the root variant of a multi-variant group promotes the oldest
    survivor to root; the response ``root_id`` reports the resulting root.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    anchor = await get_message_by_id(mid)
    if anchor is None or anchor["conversation_id"] != cid:
        raise HTTPException(status_code=404, detail="Message not found in conversation")
    scope = body.get("scope") if isinstance(body, dict) else None
    if scope not in ("variant", "group"):
        raise HTTPException(status_code=400, detail="scope must be 'variant' or 'group'")
    att = await get_workflow_attachment_by_id(aid)
    if att is None or att["message_id"] != mid:
        raise HTTPException(status_code=404, detail="Attachment not found on this message")
    root_id = att["parent_attachment_id"] or aid
    try:
        async with _workflow_root_lock(root_id):
            result = await delete_workflow_attachments(aid, scope=scope, expected_message_id=mid)
    except LookupError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return result


@router.post("/api/conversations/{cid}/workflow-attachments/access")
async def api_record_workflow_attachment_access(cid: str, body: dict = Body(default={})):  # noqa: B008
    """Record access events for workflow attachments.

    Body shape: ``{"ids": [int, ...]}``. Counter values are assigned in
    input-list order, so callers can encode intra-call ordering.

    Ids not belonging to this conversation are silently dropped rather
    than raising: the frontend can legitimately hold stale ids around a
    swipe / regen race, and a 400 there would be a user-visible failure
    on an ignorable client/server skew.
    """
    conv = await get_conversation(cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")

    raw_ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list of integers")

    int_ids: list[int] = []
    for v in raw_ids:
        if isinstance(v, bool):
            continue
        if isinstance(v, int):
            int_ids.append(v)

    if not int_ids:
        return {"ok": True, "recorded": 0}

    placeholders = ",".join("?" * len(int_ids))
    async with get_db() as db_conn:
        rows = list(
            await db_conn.execute_fetchall(
                f"SELECT wa.id FROM workflow_attachments wa "  # nosec B608 -- placeholders only
                f"JOIN messages m ON m.id = wa.message_id "
                f"WHERE m.conversation_id = ? AND wa.id IN ({placeholders})",
                (cid, *int_ids),
            )
        )
    valid_ids_set = {r["id"] for r in rows}
    ordered_valid = [i for i in int_ids if i in valid_ids_set]

    await record_access(ordered_valid)
    return {"ok": True, "recorded": len(ordered_valid)}
