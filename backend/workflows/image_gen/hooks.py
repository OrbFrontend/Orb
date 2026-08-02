"""Workflow integration for on-demand image generation, on any configured source."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from ..contracts import WorkflowEventStream
from ..toolkit import (
    build_offturn_prefix,
    get_message_by_id,
    get_workflow_character_state,
    get_workflow_config,
    insert_workflow_attachment,
    set_workflow_character_state,
)
from . import pov as pov_mod
from .composer import assemble_prompts, compose_scene
from .config import (
    MAX_REFERENCE_IMAGE_B64,
    REFERENCE_MIMES,
    WORKFLOW_ID,
    normalize_config,
    normalize_profile,
    resolve_style,
)
from .engine import (
    ImageGenerationError,
    ImageRequest,
    ProgressCallback,
    comfy_adapter,
    get_adapter,
    list_sources,
    resolve_and_generate,
)
from .engine.providers import provider_catalogue
from .references import refetch_references, resolve_references

logger = logging.getLogger(__name__)
SEED_MODULUS = 2**64
# Bounds the `node_types` sweep, so a malformed import cannot fan out unbounded.
MAX_INSPECTED_CLASS_TYPES = 200


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


def _requested_style_id(body: Any, config: Mapping[str, Any]) -> str:
    """Use an explicit request style, otherwise today's global selection."""
    style_id = body.get("style_id") if isinstance(body, Mapping) else None
    return style_id or config["default_style"]


def _phase(label: str) -> dict:
    # No `channel`: the one consumer (the Visualize modal) keys the phase pill per
    # message id, so a channel written here could only disagree with it.
    return {"event": "phase_status", "data": {"label": label}}


def _terminal(attachment_id: int | None, error: str | None) -> list[dict]:
    """The events every generate stream ends on, success or failure.

    Clients finish on `image_gen_done`, not on stream close, so this sequence is
    the contract: at most one error, the phase reset, then the terminal event.
    Transport-neutral; the API layer serializes them to SSE frames.
    """
    events: list[dict] = [{"event": "image_gen_error", "data": {"message": error}}] if error else []
    events.append({"event": "phase_status", "data": {"state": "done"}})
    events.append({"event": "image_gen_done", "data": {"attachment_id": attachment_id}})
    return events


def _failed_stream(message: str) -> WorkflowEventStream:
    """A guard rejection, on the same wire a render failure uses.

    A bare `{"error": ...}` dict leaves the client parsing JSON as SSE: no frames,
    no terminal event, and a button that silently re-enables with nothing shown.
    """

    async def events():
        for event in _terminal(None, message):
            yield event

    return WorkflowEventStream(events=events())


def _progress_label(stage: str, detail: Mapping[str, Any]) -> str | None:
    """Render an adapter progress event as a user-facing phase label."""
    if stage == "uploading":
        return "Uploading reference image..."
    if stage == "rendering":
        # A cloud adapter names itself; ComfyUI's wording is the fallback.
        backend = detail.get("backend")
        return f"Rendering on {backend}..." if isinstance(backend, str) and backend else "Rendering in ComfyUI..."
    if stage == "queued":
        ahead = detail.get("ahead")
        if isinstance(ahead, int) and not isinstance(ahead, bool) and ahead > 0:
            return f"Queued behind {ahead} render{'s' if ahead > 1 else ''}..."
        return "Queued on ComfyUI..."
    return None


def _history_through(history: Sequence[Mapping[str, Any]], message_id: int) -> list[dict]:
    """History up to and including the anchor message.

    Raises when the anchor is not on it. `get_message_by_id` proves conversation
    membership but not branch membership, so a message on an inactive branch would
    otherwise compose from replies that came *after* the one being visualized.
    """
    result: list[dict] = []
    for msg in history:
        result.append(dict(msg))
        if msg.get("id") == message_id:
            return result
    raise ValueError("that message is not on this conversation's active branch")


def _metadata(
    *,
    config: Mapping[str, Any],
    style: Mapping[str, Any],
    result,
    prompt: str,
    negative_prompt: str,
    composer_mode: str,
    pov: str,
    pov_source: str,
) -> dict:
    info = dict(result.backend_info)
    return {
        "source": config["source"],
        "style_id": style["id"],
        "recipe_id": None,
        "workflow_id": info.get("workflow_id"),
        "bundle_id": None,
        "runtime_version": None,
        "backend_model": info.get("backend_model"),
        "composer_mode": composer_mode,
        # Which camera was drawn and which lever chose it: a wrong POV must be
        # traceable to manual/classifier/default rather than guessed at.
        "pov": pov,
        "pov_source": pov_source,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        # A reroll re-fetches strictly by these origins, so only the seed moves.
        "references": info.get("references") or [],
        # Whether the stored seed means anything. A seedless API is nondeterministic,
        # so the attachment says so rather than printing a hex the render never saw.
        # The seed is still minted and stored -- rehydrate 409s on a null one.
        "seed_honored": info.get("seed_honored") is not False,
        # Read back off the graph that executed, so replay compares what an image
        # was actually rendered with rather than what its ids imply.
        **{key: info.get(key) for key in ("width", "height", "steps", "cfg", "sampler", "scheduler")},
    }


def _consumption(
    style: Mapping[str, Any],
    prompt: str,
    negative_prompt: str,
    result=None,
    record: Mapping[str, Any] | None = None,
    *,
    source_label: str = "External ComfyUI",
) -> dict:
    info: Mapping[str, Any] = getattr(result, "backend_info", {}) if result is not None else {}
    notes = list(info.get("notes") or [])
    payload = {
        "source": source_label,
        "style_id": style["id"],
        "style_label": style["label"],
        "prompt": prompt,
        "negative_prompt": negative_prompt,
    }
    # In the provider's own unit, never converted: renaming an undocumented unit to
    # "usd" would pick a divisor by omission and print a wrong billing figure.
    cost = info.get("cost")
    if isinstance(cost, Mapping) and cost.get("value") is not None:
        payload["cost"] = dict(cost)
    if info.get("seed_honored") is False:
        payload["seed_honored"] = False
    # The camera and references ride both halves: generation_metadata is the replay
    # record the UI never reads, and a wrong POV or reference is exactly the failure
    # a user needs traced while looking at the bad image. *record* is whichever dict
    # already carries them -- fresh metadata on a generate, stored params on a reroll.
    for key in ("pov", "pov_source"):
        value = (record or {}).get(key)
        if value:
            payload[key] = value
    references = (record or {}).get("references")
    if isinstance(references, (list, tuple)) and references:
        payload["references"] = [
            {key: entry.get(key) for key in ("slot", "source", "origin")} for entry in references if isinstance(entry, Mapping)
        ]
    # Disclosure lives in the display-safe half, on the attachment being looked at.
    if notes:
        payload["notes"] = notes
    return payload


def _recorded_references(params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The reference records on a stored image, as a list this hook can count."""
    recorded = params.get("references")
    if not isinstance(recorded, (list, tuple)):
        return []
    return [entry for entry in recorded if isinstance(entry, Mapping)]


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
    progress: ProgressCallback | None = None,
):
    history = _history_through(ctx.history, int(message["id"]))
    if prefix is None:
        prefix = await build_offturn_prefix(ctx.conversation_id, history, ctx.settings, lane="agent")
    selected_style = resolve_style(config, style_id)
    adapter = get_adapter(config)
    # Resolved once, up front: the composer needs its negative-prompt answer,
    # references need its slot list, and the render needs the target itself.
    target = adapter.resolve_target(selected_style, None)
    character = getattr(ctx, "character", None)
    profile_owner_name = str(character.get("name") or "") if isinstance(character, Mapping) else ""
    appearance = str(profile.get("appearance_prompt") or "")
    # Above the engine, because this reads conversation state. **Before** the
    # composer, because the composer must know whether the image model will be
    # looking at a reference, and because a required slot with nothing to fill it
    # should fail before a full prompt-composition call is paid for.
    references = await resolve_references(
        target.reference_slots,
        history=history,
        anchor_id=int(message["id"]),
        character_id=getattr(ctx, "character_id", None),
        profile=profile,
    )
    # Only an *optional* slot can come back short -- see `references._required`.
    unfilled = len(target.reference_slots) - len(references)
    # Here, not in the caller's prep phase: the classifier's first call loads a
    # model, and that latency belongs behind the "Composing..." pill.
    pov, pov_source = await pov_mod.resolve(mode=config["pov_mode"], history=history)
    logger.info("[image_gen] camera: %s (from %s)", pov, pov_source)
    scene, avoid, composer_mode = await compose_scene(
        client=ctx.agent_client,
        model_name=ctx.agent_model_name,
        prefix=prefix,
        settings=ctx.settings,
        prompt_format=selected_style["prompt_format"],
        pov=pov,
        reasoning_on=bool(config.get("prompter_reasoning")),
        scene_analysis=bool(config.get("scene_analysis")),
        appearance=appearance,
        profile_owner_name=profile_owner_name,
        extra_instructions=str(selected_style.get("extra_instructions") or ""),
        supports_negative=target.supports_negative_prompt,
        has_references=bool(references),
        style_prompt=str(selected_style.get("prompt") or ""),
        style_negative_prompt=str(selected_style.get("negative_prompt") or ""),
        profile_negative_prompt=str(profile.get("negative_prompt") or ""),
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
            references=references,
        ),
        target=target,
        progress=progress,
    )
    md = _metadata(
        config=config,
        style=style,
        result=result,
        prompt=prompt,
        negative_prompt=negative,
        composer_mode=composer_mode,
        pov=pov,
        pov_source=pov_source,
    )
    consumption = _consumption(style, prompt, negative, result, record=md, source_label=adapter.label)
    if unfilled > 0:
        # An optional slot resolving to nothing changed what was rendered, so say
        # so rather than leave it inferred from a missing Reference row.
        consumption.setdefault("notes", []).append(
            "no reference image was available for this render, so it was drawn from the prompt alone"
        )
    return _attachment(seed, result, md, consumption)


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
        result = {"ok": True, "profile": normalized}
        # The normalizer drops a reference image it cannot accept rather than
        # truncating it, so a bare "ok" would let the form preview the image,
        # report success, and show it gone on reopen with nothing to explain why.
        sent = profile.get("reference_image_b64")
        if isinstance(sent, str) and sent.strip() and not normalized["reference_image_b64"]:
            accepted = ", ".join(mime.removeprefix("image/").upper() for mime in REFERENCE_MIMES)
            result["warning"] = (
                f"That reference image was not saved: Orb accepts {accepted} files up to "
                f"{MAX_REFERENCE_IMAGE_B64 * 3 // 4 // (1024 * 1024)} MB."
            )
        return result
    return {"error": f"unknown action: {action!r}"}


# --- QUERY: conversation-less config / capability discovery -------------------
# These back the tools-panel card and the settings form. They answer from the saved
# config or by probing the backend, with no conversation in scope, and report their
# own failures in-band as ``{"error": ...}`` -- the caller degrades (empty model
# list, plain-text fields) rather than treating a probe failure as an HTTP error.


async def _config_from_query(body) -> dict:
    """The form's unsaved override if the body carries one, else the saved slot.

    The settings form tests and inspects a config it has not saved yet; the
    tools-panel card sends none.
    """
    if isinstance(body, dict) and isinstance(body.get("config"), dict):
        return normalize_config(body["config"])
    return normalize_config(await get_workflow_config(WORKFLOW_ID))


async def query(ctx, body):
    action = body.get("action") if isinstance(body, dict) else None
    if action == "status":
        return await _status(body)
    if action == "styles":
        return await _styles(body)
    if action == "test":
        return await _test_connection(body)
    if action == "models":
        return await _external_models(body)
    if action == "node_types":
        return await _node_types(body)
    return {"error": f"unknown action: {action!r}"}


async def _status(body) -> dict:
    config = await _config_from_query(body)
    external = config["external_comfy"]
    adapter = get_adapter(config)
    return {
        "source": config["source"],
        "capabilities": dict(adapter.capabilities),
        # The source picker, the provider dropdown and the capability line all read
        # from here, so the three cannot disagree. `providers` is the preset table
        # *projected* -- no configured api_key may enter this payload.
        "sources": list_sources(),
        "providers": provider_catalogue(),
        "api_url": external["api_url"],
        "default_style": config["default_style"],
        # The camera picker labels "Auto" off these two. Both are local, so they
        # ride this answer instead of costing a second round trip.
        "classifier_ready": await pov_mod.classifier_ready(),
        "fallback_mode": pov_mod.DEFAULT_POV_MODE,
        "style_count": len(config["styles"]),
        "user_graph_count": len(external["user_graphs"]),
        **adapter.readiness(),
        "managed_local": {
            "available": False,
            "reason": "Managed local image generation is not included in this stage",
        },
    }


async def _styles(body) -> dict:
    config = await _config_from_query(body)
    return {
        "source": config["source"],
        "default_style": config["default_style"],
        "styles": config["styles"],
    }


async def _test_connection(body) -> dict:
    # Only the readiness probe (which sends no config) may answer from the cached
    # node catalogue -- pressing Test means "look again".
    explicit = isinstance(body, dict) and isinstance(body.get("config"), dict)
    config = await _config_from_query(body)
    try:
        return await get_adapter(config).validate_connection(allow_cached=not explicit)
    except (ImageGenerationError, ValueError) as exc:
        return {"error": str(exc)}


async def _external_models(body) -> dict:
    config = await _config_from_query(body)
    try:
        return {"models": await get_adapter(config).list_models()}
    except ImageGenerationError as exc:
        return {"error": str(exc)}


async def _node_types(body) -> dict:
    """Slot-role typing for the node classes in a graph the user is importing.

    Takes class-type names, not the graph: the browser already parsed it. Dispatches
    to the ComfyUI adapter **explicitly, never by active source** -- imported graphs
    are global and the importer stays usable under cloud. A connection failure
    degrades to no typing; the picker falls back to conventional input names.
    """
    raw = body.get("class_types") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return {"error": "class_types (list of strings) required"}
    class_types = [item for item in raw if isinstance(item, str) and item][:MAX_INSPECTED_CLASS_TYPES]
    config = await _config_from_query(body)
    adapter = comfy_adapter(config)
    try:
        return {"nodes": await adapter.node_roles(class_types)}
    except ImageGenerationError:
        return {"nodes": {}}


async def _generate_response(ctx, body) -> WorkflowEventStream:
    mid = body.get("message_id")
    if not isinstance(mid, int) or isinstance(mid, bool):
        return _failed_stream("message_id (int) required")
    message = await get_message_by_id(mid)
    if message is None or message.get("conversation_id") != ctx.conversation_id:
        return _failed_stream("That message is no longer part of this conversation")
    if message.get("role") != "assistant":
        return _failed_stream("Images can only be generated for assistant messages")
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    style_id = _requested_style_id(body, config)
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    # The stream body runs after the trigger route releases its workflow locks, so
    # every DB-backed prefix component is rebuilt now and captured into the
    # generator; rendering itself stays unlocked.
    try:
        resolve_style(config, style_id)
        history = _history_through(ctx.history, mid)
    except ValueError as exc:
        return _failed_stream(str(exc))
    prefix = await build_offturn_prefix(ctx.conversation_id, history, ctx.settings, lane="agent")

    async def stream():
        attachment_id: int | None = None
        error: str | None = None
        labels: asyncio.Queue = asyncio.Queue()

        def on_progress(stage: str, detail: Mapping[str, Any]) -> None:
            label = _progress_label(stage, detail)
            if label:
                labels.put_nowait(label)

        # A task, not an inline await: progress must reach the wire while the render
        # is still in flight, or every label sits behind a minute-long call.
        task = asyncio.create_task(
            _generate_fresh(
                ctx=ctx,
                message=message,
                config=config,
                profile=profile,
                style_id=style_id,
                prefix=prefix,
                progress=on_progress,
            )
        )
        try:
            yield _phase("Composing image prompt...")
            while not task.done():
                try:
                    label = await asyncio.wait_for(labels.get(), 0.5)
                except TimeoutError:
                    continue
                yield _phase(label)
            while not labels.empty():
                yield _phase(labels.get_nowait())
            attachment_id, rejected = await insert_workflow_attachment(mid, await task)
            if attachment_id is None:
                error = (rejected or {}).get("reason") or "attachment rejected"
        except (ImageGenerationError, ValueError) as exc:
            logger.warning("image generation failed for message %s: %s", mid, exc)
            error = str(exc)
        except Exception:
            logger.exception("image generation failed for message %s", mid)
            error = "Image generation failed"
        finally:
            # Teardown only -- never a yield. A client disconnecting mid-render
            # throws GeneratorExit at the suspended yield above, and yielding under
            # that raises "async generator ignored GeneratorExit" with no reader
            # left anyway. Cancelling keeps the render from outliving the request.
            task.cancel()
        for event in _terminal(attachment_id, error):
            yield event

    return WorkflowEventStream(events=stream())


async def regenerate(ctx, body):
    message = await get_message_by_id(ctx.message_id)
    if message is None or message.get("role") != "assistant":
        return []
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    # Full regenerate recomposes from current settings. Only reroll/rehydrate
    # replay the predecessor attachment's stored generation parameters.
    style_id = _requested_style_id(body, config)
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    # RegenCtx history excludes the anchor; append it before rebuilding the prefix.
    ctx_with_history = _RegenCompositionCtx(ctx, tuple(list(ctx.history) + [message]))
    # Deliberately unguarded: an empty batch reads to the route as a successful
    # regenerate with nothing in it, so the button appears to do nothing. Every
    # error this raises is one the user can act on, and the route surfaces it.
    return [
        await _generate_fresh(
            ctx=ctx_with_history,
            message=message,
            config=config,
            profile=profile,
            style_id=style_id,
        )
    ]


class _RegenCompositionCtx:
    def __init__(self, ctx, history):
        self.conversation_id = ctx.conversation_id
        self.history = history
        self.settings = ctx.settings
        self.agent_client = ctx.agent_client
        self.agent_model_name = ctx.agent_model_name
        self.character = ctx.character
        # Carried for reference resolution: `character` reads the card by id.
        self.character_id = ctx.character_id


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
    # The style first: it is the one step that can reject, and discovering it was
    # deleted after a full render wastes a minute.
    style = resolve_style(config, style_id)
    # An override that changed the style retargets the render, so the stored graph
    # and checkpoint pins -- which describe the OLD style -- go. Popped from `params`
    # itself, not a copy, so the sibling the route persists records no stale pins.
    # `references` is deliberately kept: those two are pins, a recorded reference is
    # an *origin* this ctx can re-fetch with no history, and `refetch_references`
    # re-keys it onto the new target's slots.
    prior_style = (ctx.prior_consumption_metadata or {}).get("style_id")
    style_changed = bool(prior_style) and prior_style != style_id
    if style_changed:
        params.pop("workflow_id", None)
        params.pop("backend_model", None)
    # **After** those pops: a target resolved above them would answer off the
    # previous style's record and re-render the old graph.
    adapter = get_adapter(config)
    target = adapter.resolve_target(style, params)
    # On top of the adapter's own: `target.notes` already reaches the attachment
    # through backend_info, so repeating them here would print each one twice.
    notes: list[str] = []
    # A stored image rendered on another backend cannot be reproduced by this one.
    # Re-rendering and disclosing beats refusing, which surfaces only as a 500.
    recorded_source = params.get("source")
    if isinstance(recorded_source, str) and recorded_source and recorded_source != config["source"]:
        was = next((s["label"] for s in list_sources() if s["id"] == recorded_source), recorded_source)
        notes.append(f"this image was generated on {was}; it has been re-rendered on {adapter.label}, so it will not match")
    # Rehydrate promises the *same bytes back*, which a seedless API cannot give.
    # The discriminator is the seed, not the eviction marker: /rehydrate hands back
    # the row's own stored seed, /reroll-gen mints a fresh one, and the widget puts
    # a reroll button on the evicted card one click away.
    if not target.supports_seed and str(seed) == str(ctx.original_attachment.get("seed") or ""):
        notes.append(
            "this provider does not accept a seed, so the original image could not be restored exactly; "
            "this is a fresh render of the same prompt, and it was billed as one"
        )
    # Strictly by recorded origin: a reroll promises only the seed changes, so
    # re-resolving from a branch that may have moved on is what must not happen.
    recorded_references = _recorded_references(params)
    if recorded_references and not target.reference_slots:
        # This style takes no reference images. Submitting them anyway is what sent
        # a stored WebP into an edit endpoint that had declared PNG/JPEG, and
        # dropping them silently is the substitution this workflow refuses to make.
        references = ()
        notes.append(
            "this style does not take reference images, so the reference the original used was not sent; "
            "the picture will not match"
        )
    else:
        references = await refetch_references(recorded_references, slots=target.reference_slots)
        dropped = len(recorded_references) - len(references)
        if dropped > 0:
            notes.append(f"this style takes fewer reference images than the original, so {dropped} of them were not sent")
    # `params` is what the route stores as the sibling's generation_metadata, so it
    # records what was actually sent, re-keyed slots and all -- leaving the previous
    # target's slot ids would make the next reroll re-key off a record never true.
    if recorded_references or references:
        params["references"] = [reference.record() for reference in references]
    resolved_seed = fold_seed(seed)
    result = await resolve_and_generate(
        config,
        ImageRequest(
            prompt=prompt,
            negative_prompt=negative,
            seed=resolved_seed,
            style_id=style_id,
            timeout_seconds=config["timeout_seconds"],
            references=references,
        ),
        # Never through the style: that would re-render an old attachment on
        # whatever checkpoint the style points at today.
        target=target,
    )
    # The camera cannot have changed under a new seed, so carry what `params`
    # already records rather than re-resolving it.
    consumption = _consumption(style, prompt, negative, result, record=params, source_label=adapter.label)
    if notes:
        consumption["notes"] = [*notes, *consumption.get("notes", [])]
    if style_changed:
        # Only the assembled prompt is stored, never the scene/avoid halves, so a
        # style swap cannot re-word it -- say so rather than substitute silently.
        consumption.setdefault("notes", []).append(
            f"style changed to {style['label']} on reroll; the prompt text still carries the previous style's wording"
        )
    return result.image_bytes, consumption
