"""Workflow integration for on-demand external ComfyUI generation."""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any, Mapping, Sequence

from starlette.responses import StreamingResponse

from ..toolkit import (
    build_offturn_prefix,
    get_message_by_id,
    get_workflow_character_state,
    get_workflow_config,
    insert_workflow_attachment,
    set_workflow_character_state,
)
from .composer import assemble_prompts, compose_scene
from .config import WORKFLOW_ID, normalize_config, normalize_profile, resolve_style
from .engine import ImageGenerationError, ImageRequest, resolve_and_generate

logger = logging.getLogger(__name__)
SEED_MODULUS = 2**64


def fold_seed(seed: str | int) -> int:
    if isinstance(seed, bool):
        raise ValueError("invalid seed")
    if isinstance(seed, int):
        return seed % SEED_MODULUS
    value = seed.strip()
    if not value:
        raise ValueError("invalid seed")
    base = 16 if len(value) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in value) else 10
    return int(value, base) % SEED_MODULUS


def _fresh_seed() -> int:
    return fold_seed(secrets.token_hex(16))


def _frame(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, separators=(',', ':'))}\n\n"


def _history_through(history: Sequence[Mapping[str, Any]], message_id: int) -> list[dict]:
    result: list[dict] = []
    for msg in history:
        result.append(dict(msg))
        if msg.get("id") == message_id:
            break
    return result


def _metadata(
    *,
    config: Mapping[str, Any],
    style: Mapping[str, Any],
    result,
    prompt: str,
    negative_prompt: str,
    composer_mode: str,
) -> dict:
    info = dict(result.backend_info)
    return {
        "source": "external_comfy",
        "style_id": style["id"],
        "recipe_id": None,
        "workflow_id": info.get("workflow_id"),
        "bundle_id": None,
        "runtime_version": None,
        "backend_model": info.get("backend_model"),
        "composer_mode": composer_mode,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "width": None,
        "height": None,
        "steps": None,
        "cfg": None,
        "sampler": None,
        "scheduler": None,
    }


def _consumption(style: Mapping[str, Any], prompt: str, negative_prompt: str) -> dict:
    return {
        "source": "External ComfyUI",
        "style_id": style["id"],
        "style_label": style["label"],
        "prompt": prompt,
        "negative_prompt": negative_prompt,
    }


def _attachment(seed: int, result, metadata: dict, consumption: dict) -> dict:
    ext = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(result.mime, "img")
    return {
        "workflow_id": WORKFLOW_ID,
        "filename": f"generated-image.{ext}",
        "mime": result.mime,
        "data": result.image_bytes,
        "seed": str(seed),
        "generation_metadata": metadata,
        "consumption_metadata": consumption,
    }


async def _generate_fresh(
    *,
    ctx,
    message: Mapping[str, Any],
    config: Mapping[str, Any],
    profile: Mapping[str, Any],
    style_id: str,
    prefix: Sequence[dict] | None = None,
):
    if prefix is None:
        history = _history_through(ctx.history, int(message["id"]))
        prefix = await build_offturn_prefix(ctx.conversation_id, history, ctx.settings)
    scene, avoid, composer_mode = await compose_scene(
        client=ctx.client,
        prefix=prefix,
        settings=ctx.settings,
        anchor_text=str(message.get("content") or ""),
    )
    prompt, negative, style = assemble_prompts(config, style_id, profile, scene, avoid)
    seed = _fresh_seed()
    result = await resolve_and_generate(
        config,
        ImageRequest(
            prompt=prompt,
            negative_prompt=negative,
            seed=seed,
            style_id=style_id,
            timeout_seconds=config["timeout_seconds"],
        ),
    )
    md = _metadata(
        config=config,
        style=style,
        result=result,
        prompt=prompt,
        negative_prompt=negative,
        composer_mode=composer_mode,
    )
    return _attachment(seed, result, md, _consumption(style, prompt, negative))


async def on_demand(ctx, body):
    action = body.get("action") if isinstance(body, dict) else None
    if action == "generate":
        return await _generate_response(ctx, body)
    if action == "get_profile":
        if not ctx.character_id:
            return {"profile": None, "character_id": None}
        return {
            "profile": normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID)),
            "character_id": ctx.character_id,
        }
    if action == "set_profile":
        if not ctx.character_id:
            return {"error": "no active character"}
        profile = body.get("profile")
        if not isinstance(profile, dict):
            return {"error": "profile (dict) required"}
        normalized = normalize_profile(profile)
        await set_workflow_character_state(ctx.character_id, WORKFLOW_ID, normalized)
        return {"ok": True, "profile": normalized}
    return {"error": f"unknown action: {action!r}"}


async def _generate_response(ctx, body):
    mid = body.get("message_id")
    if not isinstance(mid, int) or isinstance(mid, bool):
        return {"error": "message_id (int) required"}
    message = await get_message_by_id(mid)
    if message is None or message.get("conversation_id") != ctx.conversation_id:
        return {"error": "message not found in this conversation"}
    if message.get("role") != "assistant":
        return {"error": "images can only be generated for assistant messages"}
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    style_id = body.get("style_id") or config["default_style"]
    try:
        resolve_style(config, style_id)
    except ValueError as exc:
        return {"error": str(exc)}
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    # The response body runs after the generic trigger route releases its
    # workflow locks. Rebuild every DB-backed prefix component now and capture
    # the immutable result into the generator; rendering itself stays unlocked.
    history = _history_through(ctx.history, mid)
    prefix = await build_offturn_prefix(ctx.conversation_id, history, ctx.settings)

    async def stream():
        attachment_id = None
        try:
            yield _frame(
                "phase_status",
                {
                    "channel": f"workflow:{WORKFLOW_ID}",
                    "label": "Composing image prompt...",
                },
            )
            attachment = await _generate_fresh(
                ctx=ctx,
                message=message,
                config=config,
                profile=profile,
                style_id=style_id,
                prefix=prefix,
            )
            yield _frame(
                "phase_status",
                {
                    "channel": f"workflow:{WORKFLOW_ID}",
                    "label": "Rendering in ComfyUI...",
                },
            )
            attachment_id, rejected = await insert_workflow_attachment(mid, attachment)
            if attachment_id is None:
                reason = (rejected or {}).get("reason") or "attachment rejected"
                yield _frame("image_gen_error", {"message": reason})
        except (ImageGenerationError, ValueError) as exc:
            logger.warning("image generation failed for message %s: %s", mid, exc)
            yield _frame("image_gen_error", {"message": str(exc)})
        except Exception:
            logger.exception("image generation failed for message %s", mid)
            yield _frame("image_gen_error", {"message": "Image generation failed"})
        finally:
            yield _frame("phase_status", {"channel": f"workflow:{WORKFLOW_ID}", "state": "done"})
            yield _frame("image_gen_done", {"attachment_id": attachment_id})

    return StreamingResponse(stream(), media_type="text/event-stream")


async def regenerate(ctx, body):
    message = await get_message_by_id(ctx.message_id)
    if message is None or message.get("role") != "assistant":
        return []
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    raw_original = ctx.original_attachment.get("generation_metadata")
    try:
        original = json.loads(raw_original) if isinstance(raw_original, str) else dict(raw_original or {})
    except (TypeError, ValueError):
        original = {}
    style_id = body.get("style_id") if isinstance(body, dict) else None
    style_id = style_id or original.get("style_id") or config["default_style"]
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    # RegenCtx history excludes the anchor; append it before rebuilding the prefix.
    ctx_with_history = _RegenCompositionCtx(ctx, tuple(list(ctx.history) + [message]))
    try:
        return [
            await _generate_fresh(
                ctx=ctx_with_history,
                message=message,
                config=config,
                profile=profile,
                style_id=style_id,
            )
        ]
    except Exception:
        logger.exception("image regenerate failed for attachment %s", ctx.attachment_id)
        return []


class _RegenCompositionCtx:
    def __init__(self, ctx, history):
        self.conversation_id = ctx.conversation_id
        self.history = history
        self.settings = ctx.settings
        self.client = ctx.client


async def reroll_gen(ctx, params, seed):
    if not isinstance(params, dict):
        raise ValueError("stored image parameters are missing")
    prompt = params.get("prompt")
    negative = params.get("negative_prompt")
    style_id = params.get("style_id")
    if not all(isinstance(x, str) and x for x in (prompt, style_id)) or not isinstance(negative, str):
        raise ValueError("stored image parameters are incomplete")
    assert isinstance(prompt, str)
    assert isinstance(style_id, str)
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    resolved_seed = fold_seed(seed)
    result = await resolve_and_generate(
        config,
        ImageRequest(
            prompt=prompt,
            negative_prompt=negative,
            seed=resolved_seed,
            style_id=style_id,
            timeout_seconds=config["timeout_seconds"],
        ),
    )
    style = resolve_style(config, style_id)
    return result.image_bytes, _consumption(style, prompt, negative)
