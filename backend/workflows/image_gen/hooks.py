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
    MIME_EXTENSIONS,
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
    get_adapter,
    list_sources,
    resolve_and_generate,
)
from .references import refetch_references, resolve_references

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


async def _render_inputs(ctx, body) -> tuple[dict, str, dict]:
    """What every fresh render reads before composing: `(config, style_id, profile)`.

    One place, because the on-demand and regenerate paths must answer "which style,
    which character appearance" identically -- a regenerate that resolved the style
    differently would silently re-render on another backend.
    """
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    # An explicit request style, otherwise today's global selection.
    requested = body.get("style_id") if isinstance(body, Mapping) else None
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    return config, requested or config["default_style"], profile


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


# Every render setting a *replay* reads back off a stored attachment. Read from the
# render that executed rather than from the ids that asked for it, so the record says
# what was actually drawn: `describe_render_params` reads them off the patched graph,
# and the cloud adapter probes the returned image.
_RENDER_FACTS = ("workflow_id", "backend_model", "width", "height", "steps", "cfg", "sampler", "scheduler")
# The cloud half of the same question. Absent (None) on ComfyUI, which has no such
# setting -- `replayed_text` reads a non-string as "this backend does not say".
_CLOUD_FACTS = ("quality", "reference_source")


def _render_record(result, *, source: str) -> dict:
    """What a render reported about itself, in the shape a replay reads back.

    Shared by the fresh path and the reroll path, because the sibling a reroll
    persists is itself rehydratable: a record naming the parent's target would pin
    the wrong one for every later replay of a row that never rendered on it.
    """
    info: Mapping[str, Any] = result.backend_info
    return {
        # What actually rendered, as the adapter reported it, falling back to the
        # adapter that was asked. Never `config["source"]`: that answers about the
        # *default* style, which is not necessarily the one that just rendered.
        "source": info.get("source") or source,
        **{key: info.get(key) for key in (*_RENDER_FACTS, *_CLOUD_FACTS)},
        # Whether the stored seed means anything. A seedless API is nondeterministic,
        # so the attachment says so rather than printing a hex the render never saw.
        # The seed is still minted and stored -- rehydrate 409s on a null one.
        "seed_honored": info.get("seed_honored") is not False,
    }


def _metadata(
    *,
    source: str,
    style: Mapping[str, Any],
    result,
    prompt: str,
    negative_prompt: str,
    composer_mode: str,
    pov: str,
    pov_source: str,
) -> dict:
    info: Mapping[str, Any] = result.backend_info
    return {
        **_render_record(result, source=source),
        "style_id": style["id"],
        "composer_mode": composer_mode,
        # Which camera was drawn and which lever chose it: a wrong POV must be
        # traceable to manual/classifier/default rather than guessed at.
        "pov": pov,
        "pov_source": pov_source,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        # A rehydrate re-fetches strictly by these origins, so only the bytes behind
        # them can have moved.
        "references": info.get("references") or [],
    }


def _consumption(
    style: Mapping[str, Any],
    prompt: str,
    negative_prompt: str,
    result,
    record: Mapping[str, Any],
    *,
    source_label: str,
) -> dict:
    info: Mapping[str, Any] = result.backend_info
    notes = list(info.get("notes") or [])
    payload = {
        "source": source_label,
        "style_id": style["id"],
        "style_label": style["label"],
        "prompt": prompt,
        "negative_prompt": negative_prompt,
    }
    # The size that was actually drawn, on the display-safe half so Render details can
    # show it. A resolution that silently did not take is only ever noticed by
    # eyeballing the picture against the picker, which is exactly how a stale one
    # survives; *record* carries what the render reported, not what was asked for.
    width, height = record.get("width"), record.get("height")
    if isinstance(width, int) and isinstance(height, int) and not isinstance(width, bool) and not isinstance(height, bool):
        payload["width"], payload["height"] = width, height
    # Copied through in whatever unit the provider named; see `_cost` in
    # engine/openai_image_client.py for why nothing here converts it.
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
        value = record.get(key)
        if value:
            payload[key] = value
    references = record.get("references")
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
    ext = MIME_EXTENSIONS.get(result.mime, "img")
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
    adapter = get_adapter(config, selected_style)
    # Resolved once, up front: the composer needs its negative-prompt answer,
    # references need its slot list, and the render needs the target itself. No style
    # argument -- the adapter is bound to the one the router chose it for.
    target = adapter.resolve_target(None)
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
        adapter,
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
        source=adapter.source_id,
        style=style,
        result=result,
        prompt=prompt,
        negative_prompt=negative,
        composer_mode=composer_mode,
        pov=pov,
        pov_source=pov_source,
    )
    consumption = _consumption(style, prompt, negative, result, md, source_label=adapter.label)
    if unfilled > 0:
        # An optional slot resolving to nothing changed what was rendered, so say
        # so rather than leave it inferred from a missing Reference row.
        consumption.setdefault("notes", []).append("no reference image was available, so this was drawn from the prompt alone")
    return _attachment(seed, result, md, consumption)


async def _generate_response(ctx, body) -> WorkflowEventStream:
    mid = body.get("message_id")
    if not isinstance(mid, int) or isinstance(mid, bool):
        return _failed_stream("message_id (int) required")
    message = await get_message_by_id(mid)
    if message is None or message.get("conversation_id") != ctx.conversation_id:
        return _failed_stream("That message is no longer part of this conversation")
    if message.get("role") != "assistant":
        return _failed_stream("Images can only be generated for assistant messages")
    config, style_id, profile = await _render_inputs(ctx, body)
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


async def _get_profile(ctx, _body) -> dict:
    if not ctx.character_id:
        return {"profile": None, "character_id": None}
    return {
        "profile": normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID)),
        "character_id": ctx.character_id,
    }


async def _set_profile(ctx, body) -> dict:
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


# A table rather than a branch chain, so adding an action is a single edit.
_ON_DEMAND_ACTIONS = {"generate": _generate_response, "get_profile": _get_profile, "set_profile": _set_profile}


async def on_demand(ctx, body):
    action = body.get("action") if isinstance(body, dict) else None
    handler = _ON_DEMAND_ACTIONS.get(action) if isinstance(action, str) else None
    return await handler(ctx, body) if handler else {"error": f"unknown action: {action!r}"}


async def regenerate(ctx, body):
    message = await get_message_by_id(ctx.message_id)
    if message is None or message.get("role") != "assistant":
        return []
    # Full regenerate recomposes from current settings. Only reroll/rehydrate
    # replay the predecessor attachment's stored generation parameters.
    config, style_id, profile = await _render_inputs(ctx, body)
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
    prompt, negative, style_id = params.get("prompt"), params.get("negative_prompt"), params.get("style_id")
    # Spelled as `isinstance` per field rather than an `all(...)` sweep: the sweep
    # reads the same but narrows nothing, which is what the two bare `assert
    # isinstance` lines below it were paying for.
    if not isinstance(prompt, str) or not isinstance(negative, str) or not isinstance(style_id, str):
        raise ValueError("stored image parameters are incomplete")
    if not prompt or not style_id:
        raise ValueError("stored image parameters are incomplete")
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    # The style first: it is the one step that can reject, and discovering it was
    # deleted after a full render wastes a minute.
    style = resolve_style(config, style_id)
    prior_style = (ctx.prior_consumption_metadata or {}).get("style_id")
    style_changed = bool(prior_style) and prior_style != style_id
    # The one line that separates the two routes this hook backs. A rehydrate owes
    # the row the image it lost, so the stored record picks the target -- graph,
    # checkpoint, model, size, quality, reference slot. A reroll owes the user
    # another variant of the same subject, so the *style* picks all of it, exactly as
    # a fresh render would: changing a style's resolution and pressing the dice has
    # to render at the new resolution, or the picker is a control that does nothing.
    # Only the prompt pair carries over, which is what makes Regenerate the button
    # for "these words are wrong" and this one the button for everything else.
    #
    # `references` is not read from here either way: a recorded reference is an
    # *origin* this ctx can re-fetch with no history, and `refetch_references` re-keys
    # it onto whichever slots the resolved target turns out to have.
    #
    # Routed on `style`, not on the config's global source -- the style a rehydrate
    # replays is the one the stored image named, which need not be the default one.
    adapter = get_adapter(config, style)
    target = adapter.resolve_target(params if ctx.replay else None)
    # On top of the adapter's own: `target.notes` already reaches the attachment
    # through backend_info, so repeating them here would print each one twice.
    notes: list[str] = []
    # A stored image rendered on another backend cannot be reproduced by this one.
    # Re-rendering and disclosing beats refusing, which surfaces only as a 500.
    # Against the adapter that is about to render, not the config's global source:
    # this reroll may be on a style linked somewhere else entirely.
    recorded_source = params.get("source")
    if isinstance(recorded_source, str) and recorded_source and recorded_source != adapter.source_id:
        was = next((s["label"] for s in list_sources() if s["id"] == recorded_source), recorded_source)
        notes.append(f"made on {was}, re-rendered on {adapter.label}, so it will not match")
    # Rehydrate promises the *same bytes back*, which a seedless API cannot give. Off
    # `ctx.replay` rather than off a seed comparison: that comparison was only ever a
    # proxy for this question, and it is the reroll of an evicted card -- one click
    # away in the widget -- that it would have answered wrongly.
    if not target.supports_seed and ctx.replay:
        notes.append("this provider takes no seed: a fresh render of the same prompt, billed as one, not the original image")
    # Strictly by recorded origin, on both routes: what the reference *was* is the one
    # thing a reroll still owes the parent, and re-resolving from a branch that may
    # have moved on since would quietly visualize a different message.
    recorded_references = _recorded_references(params)
    if recorded_references and not target.reference_slots:
        # This style takes no reference images. Submitting them anyway is what sent
        # a stored WebP into an edit endpoint that had declared PNG/JPEG, and
        # dropping them silently is the substitution this workflow refuses to make.
        references = ()
        notes.append("this style does not take reference images, so the original's reference was not sent; it will not match")
    else:
        references = await refetch_references(recorded_references, slots=target.reference_slots)
        dropped = len(recorded_references) - len(references)
        if dropped > 0:
            notes.append(f"this style takes fewer reference images, so {dropped} of them were not sent")
    # `params` is what the route stores as the sibling's generation_metadata, so it
    # records what was actually sent, re-keyed slots and all -- leaving the previous
    # target's slot ids would make the next reroll re-key off a record never true.
    if recorded_references or references:
        params["references"] = [reference.record() for reference in references]
    resolved_seed = fold_seed(seed)
    result = await resolve_and_generate(
        adapter,
        ImageRequest(
            prompt=prompt,
            negative_prompt=negative,
            seed=resolved_seed,
            style_id=style_id,
            timeout_seconds=config["timeout_seconds"],
            references=references,
        ),
        # Never through the style: `target` is the resolved answer, which for a
        # rehydrate is the stored image's own target rather than today's.
        target=target,
    )
    # `params` is the sibling's generation_metadata, and that sibling is itself
    # rehydratable -- so it has to record the render that just happened, not the one
    # its parent recorded. Without this a reroll that moved to a new resolution or
    # model would hand its own later rehydrate the *parent's* target and restore an
    # image this row never made. Written on both routes so there is one answer;
    # rehydrate persists no params, so for it this is bookkeeping `_consumption` reads.
    params.update(_render_record(result, source=adapter.source_id))
    # The camera cannot have changed under a new seed, so carry what `params`
    # already records rather than re-resolving it.
    consumption = _consumption(style, prompt, negative, result, params, source_label=adapter.label)
    if notes:
        consumption["notes"] = [*notes, *consumption.get("notes", [])]
    if style_changed:
        # Only the assembled prompt is stored, never the scene/avoid halves, so a
        # style swap cannot re-word it -- say so rather than substitute silently.
        consumption.setdefault("notes", []).append(
            f"style changed to {style['label']}; the prompt still carries the previous style's wording"
        )
    return result.image_bytes, consumption
