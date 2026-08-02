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
    if stage == "uploading":
        return "Uploading reference image..."
    if stage == "rendering":
        # A cloud adapter names itself here; ComfyUI's wording is the fallback so
        # nothing about the existing stream changes.
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
        "source": config["source"],
        "style_id": style["id"],
        "recipe_id": None,
        "workflow_id": info.get("workflow_id"),
        "bundle_id": None,
        "runtime_version": None,
        "backend_model": info.get("backend_model"),
        "composer_mode": composer_mode,
        # Which camera was drawn, and which lever chose it. A wrong POV is the
        # failure this feature exists to fix, so it must be traceable to
        # manual / classifier / default rather than guessed at.
        "pov": pov,
        "pov_source": pov_source,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        # Which reference images went in, and where each came from. A reroll
        # re-fetches strictly by these origins, so only the seed moves.
        "references": info.get("references") or [],
        # Whether the seed on the row means anything. A seedless API is
        # nondeterministic, so the attachment must say "not used by this provider"
        # rather than print a hex the render never saw. The seed is still minted and
        # stored: rehydrate 409s on a null one, so a null would make cloud images
        # permanently unrehydratable.
        "seed_honored": info.get("seed_honored") is not False,
        # Read back off the graph that executed, so replay can compare what an
        # image was actually rendered with rather than what its ids imply.
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
    # Carried in the provider's own unit, never converted. A cloud source is the
    # first place in Orb where clicking reroll spends money, and renaming an
    # undocumented unit to "usd" would pick a divisor by omission and print a wrong
    # number on a billing figure -- worse than printing none.
    cost = info.get("cost")
    if isinstance(cost, Mapping) and cost.get("value") is not None:
        payload["cost"] = dict(cost)
    if info.get("seed_honored") is False:
        payload["seed_honored"] = False
    # The camera and the references ride both halves. generation_metadata is the
    # replay record the UI never reads; a wrong POV or reference is exactly the
    # failure a user needs traced, so both belong where they are looking at the bad
    # image. *record* is whichever dict already carries them -- the fresh metadata
    # on a generate, the stored parameters on a reroll.
    for key in ("pov", "pov_source"):
        value = (record or {}).get(key)
        if value:
            payload[key] = value
    references = (record or {}).get("references")
    if isinstance(references, (list, tuple)) and references:
        payload["references"] = [
            {key: entry.get(key) for key in ("slot", "source", "origin")} for entry in references if isinstance(entry, Mapping)
        ]
    # Disclosure lives in the display-safe half: a replay that could not be
    # honoured exactly says so on the attachment the user is looking at.
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
    # Resolved once, up front: the composer needs this target's negative-prompt
    # answer before it writes anything, references need its slot list, and the
    # render needs the target itself. Resolving graph facts twice is how the two
    # halves come to disagree.
    target = adapter.resolve_target(selected_style, None)
    character = getattr(ctx, "character", None)
    profile_owner_name = str(character.get("name") or "") if isinstance(character, Mapping) else ""
    appearance = str(profile.get("appearance_prompt") or "")
    # Resolved from the branch as it stands, here rather than in the engine: this
    # is conversation state, and `engine/` stays ComfyUI-only. **Before** the
    # composer, for two reasons: the composer must know whether the image model
    # will be looking at a reference (an edit model takes its likeness from the
    # picture, not from the prompt), and a required slot with nothing to fill it
    # is a hard failure that should not cost a full prompt-composition call first.
    references = await resolve_references(
        target.reference_slots,
        history=history,
        anchor_id=int(message["id"]),
        character_id=getattr(ctx, "character_id", None),
        profile=profile,
    )
    # Only an *optional* slot can come back short -- see `references._required`.
    unfilled = len(target.reference_slots) - len(references)
    # Resolved here, not in the caller's prep phase: the classifier's first call
    # loads a model, and that latency belongs behind the "Composing image
    # prompt..." pill rather than ahead of the stream's first frame.
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
        # An optional slot that resolved to nothing changed what was rendered, so
        # it is disclosed on the image the user is looking at rather than left to
        # be inferred from a missing Reference row.
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
        # truncating it -- half a base64 payload is not a smaller image. Saying
        # "ok" and nothing else let the form preview an image, report success, and
        # show it gone on reopen, with nothing to explain why.
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
# These back the tools-panel card and the Advanced settings form. They answer
# from the saved workflow config or by probing the configured image backend, with
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
        # from here, so all three come from one place. `providers` is the preset
        # table *projected* -- no configured api_key may enter this payload.
        "sources": list_sources(),
        "providers": provider_catalogue(),
        "api_url": external["api_url"],
        "default_style": config["default_style"],
        # The camera picker labels "Auto" off these two: whether the classifier can
        # run, and what "Auto" degrades to when it cannot. Both are local (a file
        # check and a settings read), so they ride the readiness answer the card
        # already fetches instead of costing a second round trip.
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
    # An explicit test carries an unsaved config from the Advanced form; the
    # readiness probe behind the Visualize modal sends none. Only the latter may
    # answer from the cached node catalogue -- pressing Test means "look again".
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

    Takes class-type names rather than the graph itself: the browser already
    parsed the graph, and this only needs to know what its node classes can do.

    Dispatches to the ComfyUI adapter **explicitly, never by active source**.
    Imported graphs are global and the importer stays usable while cloud is
    selected; routing this by `config["source"]` would break it the moment the
    user switched backends. A connection failure degrades to no typing at all --
    the picker already falls back to conventional input names.
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
    # The response body runs after the generic trigger route releases its
    # workflow locks. Rebuild every DB-backed prefix component now and capture
    # the immutable result into the generator; rendering itself stays unlocked.
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
    # Deliberately unguarded. Swallowing the failure here returned an empty batch,
    # which the route reports as a successful regenerate with nothing in it -- the
    # button appears to do nothing at all. Every error this can raise is one the
    # user can act on ("generate or upload an image in this chat first"), and the
    # route logs and surfaces a raise. Let it.
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
        # `references` is deliberately **not** popped alongside them. Those two are
        # pins naming things the old style owned; a recorded reference is an
        # *origin* -- an attachment row or a card id -- which this ctx can re-fetch
        # with no history at all. What it cannot carry over is the recorded slot,
        # and `refetch_references` re-keys onto the new target's slots for exactly
        # that reason.
    # **After** the style_changed pops, never before: those two lines clear the
    # pins precisely so the new style resolves its own, and a target resolved above
    # them would answer off the previous style's record and re-render the old graph.
    adapter = get_adapter(config)
    target = adapter.resolve_target(style, params)
    # Disclosure this hook adds *on top of* the adapter's own: `target.notes` already
    # reaches the attachment through the result's backend_info, so repeating them
    # here would print each one twice.
    notes: list[str] = []
    # A stored image rendered on a different backend cannot be reproduced by this
    # one; re-rendering and disclosing beats refusing, which surfaces only as a
    # generic "reroll_gen handler raised" 500.
    recorded_source = params.get("source")
    if isinstance(recorded_source, str) and recorded_source and recorded_source != config["source"]:
        was = next((s["label"] for s in list_sources() if s["id"] == recorded_source), recorded_source)
        notes.append(f"this image was generated on {was}; it has been re-rendered on {adapter.label}, so it will not match")
    # Rehydrate promises the *same bytes back*; a seedless API returns a different
    # image and silently overwrites the evicted row with something the user never
    # generated. The discriminator is the seed, not the eviction marker: /rehydrate
    # hands back the row's own stored seed (identity), while /reroll-gen mints a
    # fresh 128-bit token. The marker would be wrong -- only /rehydrate preconditions
    # on it, and the widget puts a reroll button on the evicted card, one click away.
    if not target.supports_seed and str(seed) == str(ctx.original_attachment.get("seed") or ""):
        notes.append(
            "this provider does not accept a seed, so the original image could not be restored exactly; "
            "this is a fresh render of the same prompt, and it was billed as one"
        )
    # Strictly by recorded origin: a reroll promises only the seed changes, so
    # re-resolving from a branch that may have moved on is what must not happen.
    recorded_references = _recorded_references(params)
    if recorded_references and not target.reference_slots:
        # The style this reroll renders under takes no reference images -- the
        # connection's setting was turned off, or the graph has no mapped
        # LoadImage. Submitting them anyway is what sent a stored WebP into a
        # provider's edit endpoint that had declared PNG/JPEG; dropping them
        # silently is the substitution this workflow refuses to make. So: drop,
        # and say so on the image.
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
    # The sibling this reroll persists records what was actually sent, re-keyed
    # slots and all -- `params` is the dict the route stores as its
    # generation_metadata, so leaving the previous target's slot ids in it would
    # make the next reroll re-key from a record that was never true.
    if recorded_references or references:
        params["references"] = [
            {
                "slot": list(reference.slot),
                "source": reference.source,
                "origin": reference.origin,
                "digest": reference.digest,
                "source_digest": reference.source_digest,
            }
            for reference in references
        ]
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
        # Reroll and rehydrate reproduce a stored image's parameters. Routing
        # them through the style would re-render an old attachment on whatever
        # checkpoint that style points at today -- for rehydrate, silently
        # overwriting the row with different bytes than it is meant to restore.
        target=target,
    )
    # A reroll re-renders the stored prompt under a new seed, so the camera cannot
    # have changed: carry the one `params` already records rather than re-resolving.
    consumption = _consumption(style, prompt, negative, result, record=params, source_label=adapter.label)
    if notes:
        consumption["notes"] = [*notes, *consumption.get("notes", [])]
    if style_changed:
        # Only the assembled prompt is stored, never the scene/avoid halves it was built
        # from, so a style swap cannot re-word it -- say so rather than substitute silently.
        consumption.setdefault("notes", []).append(
            f"style changed to {style['label']} on reroll; the prompt text still carries the previous style's wording"
        )
    return result.image_bytes, consumption
