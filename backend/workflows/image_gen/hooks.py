"""Workflow integration for on-demand external ComfyUI generation."""

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
    get_workflow_state,
    insert_workflow_attachment,
    set_workflow_character_state,
    set_workflow_state,
)
from . import pov as pov_mod
from .composer import assemble_prompts, compose_scene
from .config import (
    WORKFLOW_ID,
    normalize_config,
    normalize_profile,
    resolve_style,
)
from .engine import (
    CAPABILITIES,
    ImageGenerationError,
    ImageRequest,
    ProgressCallback,
    graph_has_negative,
    list_models,
    node_roles,
    resolve_and_generate,
    validate_connection,
)

logger = logging.getLogger(__name__)
SEED_MODULUS = 2**64
# Upper bound on class-type names the `node_types` query inspects per graph, so a
# hostile or malformed import cannot fan out into an unbounded object_info sweep.
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


async def _read_pov_mode(conversation_id: str) -> str:
    """This conversation's manual camera choice. Style is global; the camera is not
    -- narration POV is a property of the chat, so it must not follow the user
    into the next one."""
    state = await get_workflow_state(conversation_id, WORKFLOW_ID)
    return pov_mod.normalize_mode((state or {}).get("pov_mode"))


def _phase(label: str) -> dict:
    # No `channel`: this stream has exactly one consumer, the Visualize modal,
    # and it keys the phase pill per message id. A channel written here could
    # only disagree with the one the client already owns.
    return {"event": "phase_status", "data": {"label": label}}


def _terminal(attachment_id: int | None, error: str | None) -> list[dict]:
    """The events every generate stream ends on, success or failure.

    Clients finish on `image_gen_done` rather than on stream close, so this
    sequence is the contract: at most one error, then the phase reset, then the
    terminal event carrying the new attachment id or null. These are
    transport-neutral event dicts; the API layer serializes them to SSE frames.
    """
    events: list[dict] = [{"event": "image_gen_error", "data": {"message": error}}] if error else []
    events.append({"event": "phase_status", "data": {"state": "done"}})
    events.append({"event": "image_gen_done", "data": {"attachment_id": attachment_id}})
    return events


def _failed_stream(message: str) -> WorkflowEventStream:
    """A guard rejection, delivered over the same wire as a render failure.

    Returning a bare `{"error": ...}` dict here would leave the client parsing a
    JSON body as SSE: it finds no frames, sees no terminal event, and silently
    re-enables its button with nothing shown to the user. The event stream keeps
    the guard rejection on the same wire the successful render uses.
    """

    async def events():
        for event in _terminal(None, message):
            yield event

    return WorkflowEventStream(events=events())


def _progress_label(stage: str, detail: Mapping[str, Any]) -> str | None:
    """Render an adapter progress event as a user-facing phase label."""
    if stage == "rendering":
        return "Rendering in ComfyUI..."
    if stage == "queued":
        ahead = detail.get("ahead")
        if isinstance(ahead, int) and not isinstance(ahead, bool) and ahead > 0:
            return f"Queued behind {ahead} render{'s' if ahead > 1 else ''}..."
        return "Queued on ComfyUI..."
    return None


def _history_through(history: Sequence[Mapping[str, Any]], message_id: int) -> list[dict]:
    """History up to and including the anchor message.

    Raises when the anchor is not on this history. `get_message_by_id` proves
    conversation membership but not branch membership, so a message on an
    inactive branch would otherwise fall through to "the whole active path" --
    composing the image from replies that came *after* the one being visualized.
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
        "source": "external_comfy",
        "style_id": style["id"],
        "recipe_id": None,
        "workflow_id": info.get("workflow_id"),
        "bundle_id": None,
        "runtime_version": None,
        "backend_model": info.get("backend_model"),
        "composer_mode": composer_mode,
        # Which camera was drawn, and which lever chose it. A wrong POV is the
        # failure this feature exists to fix, so it must be traceable to
        # character_tag / manual / classifier / default rather than guessed at.
        "pov": pov,
        "pov_source": pov_source,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        # Read back off the graph that executed, so replay can compare what an
        # image was actually rendered with rather than what its ids imply.
        **{key: info.get(key) for key in ("width", "height", "steps", "cfg", "sampler", "scheduler")},
    }


def _consumption(
    style: Mapping[str, Any], prompt: str, negative_prompt: str, result=None, camera: Mapping[str, Any] | None = None
) -> dict:
    notes = list(getattr(result, "backend_info", {}).get("notes") or []) if result is not None else []
    payload = {
        "source": "External ComfyUI",
        "style_id": style["id"],
        "style_label": style["label"],
        "prompt": prompt,
        "negative_prompt": negative_prompt,
    }
    # The camera rides both halves. generation_metadata is the replay record the
    # UI never reads; a wrong POV is the failure this feature exists to fix, so
    # which camera ran and which lever chose it belong where the user is looking
    # at the bad image. *camera* is whichever dict already carries them -- the
    # fresh metadata on a generate, the stored parameters on a reroll.
    for key in ("pov", "pov_source"):
        value = (camera or {}).get(key)
        if value:
            payload[key] = value
    # Disclosure lives in the display-safe half: a replay that could not be
    # honoured exactly says so on the attachment the user is looking at.
    if notes:
        payload["notes"] = notes
    return payload


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
    pov_mode: str,
    prefix: Sequence[dict] | None = None,
    progress: ProgressCallback | None = None,
):
    history = _history_through(ctx.history, int(message["id"]))
    if prefix is None:
        prefix = await build_offturn_prefix(ctx.conversation_id, history, ctx.settings, lane="agent")
    selected_style = resolve_style(config, style_id)
    character = getattr(ctx, "character", None)
    profile_owner_name = str(character.get("name") or "") if isinstance(character, Mapping) else ""
    appearance = str(profile.get("appearance_prompt") or "")
    # Resolved here, not in the caller's prep phase: the classifier's first call
    # loads a model, and that latency belongs behind the "Composing image
    # prompt..." pill rather than ahead of the stream's first frame.
    pov, pov_source = await pov_mod.resolve(appearance=appearance, mode=pov_mode, history=history)
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
        supports_negative=graph_has_negative(config, selected_style["workflow"]),
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
    return _attachment(seed, result, md, _consumption(style, prompt, negative, result, camera=md))


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
    # The camera picker. Both run under the trigger route's workflow_state lock,
    # which is why neither acquires it -- asyncio.Lock is not reentrant, and the
    # read-then-write these guard is exactly what that lock is held across.
    if action == "get_pov":
        # Readiness rides along: the picker needs both to label "Auto", and both
        # are local (a file check and a settings read), so one trip answers it.
        return {
            "pov_mode": await _read_pov_mode(ctx.conversation_id),
            "classifier_ready": await pov_mod.classifier_ready(),
        }
    if action == "set_pov":
        mode = pov_mod.normalize_mode(body.get("pov_mode"))
        state = dict(await get_workflow_state(ctx.conversation_id, WORKFLOW_ID) or {})
        state["pov_mode"] = mode
        await set_workflow_state(ctx.conversation_id, WORKFLOW_ID, state)
        return {"ok": True, "pov_mode": mode}
    return {"error": f"unknown action: {action!r}"}


# --- QUERY: conversation-less config / capability discovery -------------------
# These back the tools-panel card and the Advanced settings form. They answer
# from the saved workflow config or by probing the external ComfyUI server, with
# no conversation in scope, and report their own failures in-band as
# ``{"error": ...}`` -- the caller degrades (empty model list, plain-text fields)
# rather than treating a probe failure as an HTTP error.


async def _config_from_query(body) -> dict:
    """Normalized config the query answers against: the form's unsaved override
    if the body carries one, else the persisted slot.

    The Advanced form tests and inspects a config it has not saved yet, so a
    ``config`` in the body wins; the tools-panel card sends none and reads the
    saved slot.
    """
    if isinstance(body, dict) and isinstance(body.get("config"), dict):
        return normalize_config(body["config"])
    return normalize_config(await get_workflow_config(WORKFLOW_ID))


def _configuration_readiness(config: Mapping[str, Any]) -> dict:
    """What the saved configuration alone can tell us, with no network I/O.

    Deliberately not a health probe: the tools-panel card renders on every panel
    open, and making that wait on a remote server would trade a fast honest
    answer for a slow one. Reachability is the ``test`` action, which the
    Visualize modal already runs at the moment it matters.
    """
    external = config["external_comfy"]
    graphs = {graph["id"]: graph for graph in external["user_graphs"]}
    # External mode ships no default graph, so a style with nothing pinned cannot
    # render at all -- the first thing to fix, ahead of checkpoints.
    needs_workflow = any(not style["workflow"] for style in external["styles"])
    unresolved = sorted(
        {style["workflow"] for style in external["styles"] if style["workflow"] and style["workflow"] not in graphs}
    )
    # A checkpoint is only required when the pinned graph exposes a model slot for
    # Orb's selection to override; a self-contained graph carries its own.
    needs_checkpoint = any(
        not style["checkpoint"] and style["workflow"] in graphs and "checkpoint" in graphs[style["workflow"]]["slots"]
        for style in external["styles"]
    )
    if needs_workflow:
        return {
            "ready": False,
            "reason": "no_workflow",
            "detail": "Import a ComfyUI workflow and assign it to each style",
        }
    if unresolved:
        return {"ready": False, "reason": "unknown_workflow", "detail": f"Unknown workflow: {', '.join(unresolved)}"}
    if needs_checkpoint:
        return {
            "ready": False,
            "reason": "no_checkpoint",
            "detail": "Choose a checkpoint in settings before generating",
        }
    return {"ready": True, "reason": "", "detail": f"External ComfyUI at {external['api_url']}"}


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
    return {
        "source": "external_comfy",
        "capabilities": dict(CAPABILITIES),
        "configured": any(s["checkpoint"] or s["workflow"] for s in external["styles"]),
        "api_url": external["api_url"],
        "default_style": config["default_style"],
        "style_count": len(external["styles"]),
        "user_graph_count": len(external["user_graphs"]),
        **_configuration_readiness(config),
        "managed_local": {
            "available": False,
            "reason": "Managed local image generation is not included in this stage",
        },
    }


async def _styles(body) -> dict:
    config = await _config_from_query(body)
    return {
        "source": "external_comfy",
        "default_style": config["default_style"],
        "styles": config["external_comfy"]["styles"],
    }


async def _test_connection(body) -> dict:
    # An explicit test carries an unsaved config from the Advanced form; the
    # readiness probe behind the Visualize modal sends none. Only the latter may
    # answer from the cached node catalogue -- pressing Test means "look again".
    explicit = isinstance(body, dict) and isinstance(body.get("config"), dict)
    config = await _config_from_query(body)
    try:
        return await validate_connection(config, allow_cached=not explicit)
    except (ImageGenerationError, ValueError) as exc:
        return {"error": str(exc)}


async def _external_models(body) -> dict:
    config = await _config_from_query(body)
    try:
        return {"models": await list_models(config)}
    except ImageGenerationError as exc:
        return {"error": str(exc)}


async def _node_types(body) -> dict:
    """Slot-role typing for the node classes in a graph the user is importing.

    Takes class-type names rather than the graph itself: the browser already
    parsed the graph, and this only needs to know what its node classes can do.
    """
    raw = body.get("class_types") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return {"error": "class_types (list of strings) required"}
    class_types = [item for item in raw if isinstance(item, str) and item][:MAX_INSPECTED_CLASS_TYPES]
    config = await _config_from_query(body)
    try:
        return {"nodes": await node_roles(config, class_types)}
    except ImageGenerationError as exc:
        return {"error": str(exc)}


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
    # The response body runs after the generic trigger route releases its
    # workflow locks. Rebuild every DB-backed prefix component now and capture
    # the immutable result into the generator; rendering itself stays unlocked.
    try:
        resolve_style(config, style_id)
        history = _history_through(ctx.history, mid)
    except ValueError as exc:
        return _failed_stream(str(exc))
    prefix = await build_offturn_prefix(ctx.conversation_id, history, ctx.settings, lane="agent")
    # Read under the route's workflow_state lock, with the rest of the DB-backed
    # prep; the stream body below runs after that lock releases.
    pov_mode = await _read_pov_mode(ctx.conversation_id)

    async def stream():
        attachment_id: int | None = None
        error: str | None = None
        labels: asyncio.Queue = asyncio.Queue()

        def on_progress(stage: str, detail: Mapping[str, Any]) -> None:
            label = _progress_label(stage, detail)
            if label:
                labels.put_nowait(label)

        # The render runs as a task so its progress can reach the wire while it
        # is still in flight. Awaiting it inline would hold every label behind a
        # call that takes the better part of a minute, which is what left the
        # UI showing "Composing..." for the whole render and "Rendering..." for
        # the DB insert that followed it.
        task = asyncio.create_task(
            _generate_fresh(
                ctx=ctx,
                message=message,
                config=config,
                profile=profile,
                style_id=style_id,
                pov_mode=pov_mode,
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
            # Teardown only -- never a yield. A client that disconnects mid-render
            # closes this generator, which throws GeneratorExit at the suspended
            # yield above; yielding from `finally` under that raises "async
            # generator ignored GeneratorExit" and the terminal frames have no
            # reader left anyway. Cancelling here is what keeps the render task
            # from outliving the request.
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
    try:
        return [
            await _generate_fresh(
                ctx=ctx_with_history,
                message=message,
                config=config,
                profile=profile,
                style_id=style_id,
                pov_mode=await _read_pov_mode(ctx.conversation_id),
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
        self.agent_client = ctx.agent_client
        self.agent_model_name = ctx.agent_model_name
        self.character = ctx.character


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
    # Resolve the style first: it is the one step that can reject, and spending a
    # full render before discovering the style was deleted wastes a minute.
    style = resolve_style(config, style_id)
    # An override that changed the style retargets the render. The stored graph and
    # checkpoint pins describe the OLD style, so drop them and let the new style resolve
    # its own -- popping from `params` (not a filtered copy) so the sibling the route
    # persists records no stale pins either. Data-driven, not route-driven: rehydrate
    # sends no overrides, so its style always matches and its replay stays exact.
    prior_style = (ctx.prior_consumption_metadata or {}).get("style_id")
    style_changed = bool(prior_style) and prior_style != style_id
    if style_changed:
        params.pop("workflow_id", None)
        params.pop("backend_model", None)
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
        # Reroll and rehydrate reproduce a stored image's parameters. Routing
        # them through the style would re-render an old attachment on whatever
        # checkpoint that style points at today -- for rehydrate, silently
        # overwriting the row with different bytes than it is meant to restore.
        replay=params,
    )
    # A reroll re-renders the stored prompt under a new seed, so the camera cannot
    # have changed: carry the one `params` already records rather than re-resolving.
    consumption = _consumption(style, prompt, negative, result, camera=params)
    if style_changed:
        # Only the assembled prompt is stored, never the scene/avoid halves it was built
        # from, so a style swap cannot re-word it -- say so rather than substitute silently.
        consumption.setdefault("notes", []).append(
            f"style changed to {style['label']} on reroll; the prompt text still carries the previous style's wording"
        )
    return result.image_bytes, consumption
