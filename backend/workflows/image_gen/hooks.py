"""Workflow integration for on-demand image generation, on any configured source."""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import (
    WorkflowEventStream,
    build_offturn_prefix,
    get_message_by_id,
    get_workflow_character_state,
    get_workflow_config,
    insert_workflow_attachment,
    set_workflow_character_state,
)
from . import pov as pov_mod
from . import subjects as subjects_mod
from .composer import (
    addressable_subjects,
    analyze_scene,
    assemble_prompts,
    compose_scene,
)
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
    recorded_edge,
    resolve_and_generate,
)
from .engine.contracts import ResolvedReference
from .references import (
    plan_slots,
    previous_image,
    refetch_references,
    replay_slots,
    resolve_references,
)
from .subjects import Subject

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
    requested = body.get("style_id") if isinstance(body, Mapping) else None
    profile = normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None)
    return config, requested or config["default_style"], profile


def _phase(label: str) -> dict:
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


_REPLAYED_FACTS = (
    "workflow_id",
    "backend_model",
    "width",
    "height",
    "quality",
    "reference_source",
    "negative_prompt_sent",
)
_DISCLOSED_FACTS = ("steps", "cfg", "sampler", "scheduler")


async def _rendered(adapter, request, *, target, progress=None):
    """Render through the shared seam used by fresh images and rerolls."""
    return await resolve_and_generate(adapter, request, target=target, progress=progress)


def _render_record(result, *, source: str) -> dict:
    """What a render reported about itself, in the shape a replay reads back.

    Shared by the fresh path and the reroll path, because the sibling a reroll
    persists is itself rehydratable: a record naming the parent's target would pin
    the wrong one for every later replay of a row that never rendered on it.
    """
    info: Mapping[str, Any] = result.backend_info
    return {
        "source": info.get("source") or source,
        **{key: info.get(key) for key in (*_REPLAYED_FACTS, *_DISCLOSED_FACTS)},
        "seed_honored": info.get("seed_honored") is not False,
        "size_measured": info.get("size_measured") is True,
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
        "pov": pov,
        "pov_source": pov_source,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
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
    if record.get("size_measured") is True:
        width, height = recorded_edge(record.get("width")), recorded_edge(record.get("height"))
        if width is not None and height is not None:
            payload["width"], payload["height"] = width, height
    cost = info.get("cost")
    if isinstance(cost, Mapping) and cost.get("value") is not None:
        payload["cost"] = dict(cost)
    if info.get("seed_honored") is False:
        payload["seed_honored"] = False
    for key in ("pov", "pov_source"):
        value = record.get(key)
        if value:
            payload[key] = value
    references = record.get("references")
    if isinstance(references, (list, tuple)) and references:
        payload["references"] = [
            {key: entry.get(key) for key in ("slot", "source", "origin")} for entry in references if isinstance(entry, Mapping)
        ]
    if notes:
        payload["notes"] = notes
    return payload


def _referenced_subjects(subjects: Sequence[Subject], references: Sequence[ResolvedReference]) -> list[tuple[int, str]]:
    """Return the subjects represented by sent images."""
    by_card = {subject.card_id: subject.name for subject in subjects if subject.card_id and subject.name}
    return [
        (position, name)
        for position, reference in enumerate(references, 1)
        if reference.origin.startswith("character:")
        for name in (by_card.get(reference.origin.partition(":")[2]),)
        if name
    ]


def _referenced_cards(sent: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return the cards represented by sent likenesses."""
    return {
        card
        for entry in sent
        for origin in (entry.get("origin"),)
        if isinstance(origin, str) and origin.startswith("character:")
        for card in (origin.partition(":")[2],)
        if card
    }


def _names_phrase(names: Sequence[str]) -> str:
    """A readable list of names, bounded -- a twelve-hander must not print twelve
    names into one disclosure line."""
    if len(names) > 3:
        return f"{', '.join(names[:3])} and {len(names) - 3} others"
    if len(names) > 1:
        return f"{', '.join(names[:-1])} and {names[-1]}"
    return names[0] if names else ""


def _uncovered_note(
    addressable: Sequence[Subject],
    sent: Sequence[Mapping[str, Any]],
    declared: int,
    capacity: int,
) -> str:
    """Describe subjects that exceed the available reference slots."""
    covered = _referenced_cards(sent)
    in_frame = [subject for subject in addressable if subject.card_id and subject.name]
    uncovered = [subject.name for subject in in_frame if subject.card_id not in covered]
    if not uncovered or len(uncovered) == len(in_frame):
        return ""
    lead = f"{_names_phrase(uncovered)} {'was' if len(uncovered) == 1 else 'were'} described in the prompt rather than pictured"
    if declared < capacity:
        return f"{lead}: this style fills {declared} of its {capacity} reference slots"
    return f"{lead}: this render carries {capacity} reference image{'' if capacity == 1 else 's'}"


def _unfilled_note(unfilled: int, filled: int) -> str:
    """What an optional slot that resolved to nothing is disclosed as.

    Count-aware because a target may declare several: "drawn from the prompt alone" is
    only true when *nothing* resolved, and saying it with one of two slots filled tells
    the user the opposite of what happened.
    """
    if not filled:
        return "no reference image was available, so this was drawn from the prompt alone"
    plural = unfilled > 1
    return (
        f"{unfilled} reference {'images' if plural else 'image'} could not be resolved, "
        f"so {'they were' if plural else 'it was'} not sent"
    )


def _recorded_references(params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The reference records on a stored image, as a list this hook can count."""
    recorded = params.get("references")
    if not isinstance(recorded, (list, tuple)):
        return []
    return [entry for entry in recorded if isinstance(entry, Mapping)]


def _rendered_seed(requested: int, result) -> int:
    """The seed the backend drew with, which is not always the one it was handed: a
    ComfyUI graph's seed node can declare a range smaller than the seed Orb picked,
    and the render folds into it. Record what rendered, so the number shown next to
    the image is the one that reproduces it."""
    reported = result.backend_info.get("seed")
    return reported if isinstance(reported, int) and not isinstance(reported, bool) else requested


def _attachment(seed: int, result, metadata: dict, consumption: dict) -> dict:
    ext = MIME_EXTENSIONS.get(result.mime, "img")
    return {
        "workflow_id": WORKFLOW_ID,
        "filename": f"generated-image.{ext}",
        "mime": result.mime,
        "data": result.image_bytes,
        "seed": str(_rendered_seed(seed, result)),
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
    target = adapter.resolve_target(None)
    # The order is load-bearing, and each step depends on the one above it:
    #
    #   camera   -> how many subjects there are at all (first-person keeps one)
    #   subjects -> who this render is a picture of, primary first
    #   analysis -> which of them is actually in frame
    #   slots    -> whose likeness leaves the machine, one image per person
    #   composer -> what the prompt says about the pictures that went with it
    #
    # The analyzer sits *above* the slots rather than inside the compose call so that a
    # member who spoke in the round but walked out of the shot never has their face
    # uploaded: an edit model handed a likeness the prompt never mentions draws that
    # person back in. `addressable_subjects` is the join, and it costs no extra call.
    pov, pov_source = await pov_mod.resolve(mode=config["pov_mode"], history=history)
    logger.info("[image_gen] camera: %s (from %s)", pov, pov_source)
    subjects = await subjects_mod.resolve(
        conversation_id=ctx.conversation_id,
        history=history,
        anchor_id=int(message["id"]),
        character_id=getattr(ctx, "character_id", None),
        character=getattr(ctx, "character", None),
        profile=profile,
    )
    analysis = (
        await analyze_scene(
            client=ctx.agent_client,
            model_name=ctx.agent_model_name,
            prefix=prefix,
            settings=ctx.settings,
            pov=pov,
            reasoning_on=bool(config.get("prompter_reasoning")),
            subjects=subjects,
            supports_negative=target.supports_negative_prompt,
        )
        if config.get("scene_analysis")
        else None
    )
    # Hoisted rather than inlined: this is the list the render is actually *of*, so both
    # the slots and the disclosure below read the same answer rather than the wider
    # candidate list. The chat image is found once, because the plan depends on whether
    # there is one -- `previous_or_character` asks for one slot when the chat has an
    # image and one per character when it does not.
    addressable = addressable_subjects(subjects, analysis)
    previous = previous_image(history, int(message["id"]))
    slots = plan_slots(target, addressable, previous=previous)
    references = await resolve_references(slots, subjects=addressable, previous=previous)
    unfilled = len(slots) - len(references)
    scene, avoid, composer_mode = await compose_scene(
        client=ctx.agent_client,
        model_name=ctx.agent_model_name,
        prefix=prefix,
        settings=ctx.settings,
        prompt_format=selected_style["prompt_format"],
        pov=pov,
        reasoning_on=bool(config.get("prompter_reasoning")),
        analysis=analysis,
        subjects=subjects,
        extra_instructions=str(selected_style.get("extra_instructions") or ""),
        supports_negative=target.supports_negative_prompt,
        has_references=bool(references),
        referenced_subjects=_referenced_subjects(subjects, references),
        style_prompt=str(selected_style.get("prompt") or ""),
        style_negative_prompt=str(selected_style.get("negative_prompt") or ""),
        profile_negative_prompt=str(profile.get("negative_prompt") or ""),
    )
    prompt, negative, style = assemble_prompts(config, style_id, profile, scene, avoid)
    if not prompt.strip():
        raise ImageGenerationError("the composed image prompt came out empty; try generating again")
    seed = _fresh_seed()
    result = await _rendered(
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
        consumption.setdefault("notes", []).append(_unfilled_note(unfilled, len(references)))
    # Read the adapter's record rather than the plan: it is the authoritative list of
    # what the image model was given.
    uncovered = _uncovered_note(addressable, md["references"], len(slots), target.reference_capacity)
    if uncovered:
        consumption.setdefault("notes", []).append(uncovered)
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
    sent = profile.get("reference_image_b64")
    if isinstance(sent, str) and sent.strip() and not normalized["reference_image_b64"]:
        accepted = ", ".join(mime.removeprefix("image/").upper() for mime in REFERENCE_MIMES)
        result["warning"] = (
            f"That reference image was not saved: Orb accepts {accepted} files up to "
            f"{MAX_REFERENCE_IMAGE_B64 * 3 // 4 // (1024 * 1024)} MB."
        )
    return result


_ON_DEMAND_ACTIONS = {"generate": _generate_response, "get_profile": _get_profile, "set_profile": _set_profile}


async def on_demand(ctx, body):
    action = body.get("action") if isinstance(body, dict) else None
    handler = _ON_DEMAND_ACTIONS.get(action) if isinstance(action, str) else None
    return await handler(ctx, body) if handler else {"error": f"unknown action: {action!r}"}


async def regenerate(ctx, body):
    message = await get_message_by_id(ctx.message_id)
    if message is None or message.get("role") != "assistant":
        return []
    config, style_id, profile = await _render_inputs(ctx, body)
    ctx_with_history = _RegenCompositionCtx(ctx, tuple(list(ctx.history) + [message]))
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
        self.character_id = ctx.character_id


async def reroll_gen(ctx, params, seed):
    if not isinstance(params, dict):
        raise ValueError("stored image parameters are missing")
    prompt, negative, style_id = params.get("prompt"), params.get("negative_prompt"), params.get("style_id")
    if not isinstance(prompt, str) or not isinstance(negative, str) or not isinstance(style_id, str):
        raise ValueError("stored image parameters are incomplete")
    if not style_id:
        raise ValueError("stored image parameters are incomplete")
    if not prompt.strip():
        raise ImageGenerationError("this image has no prompt to render; edit the prompt and reroll again")
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    style = resolve_style(config, style_id)
    prior_style = (ctx.prior_consumption_metadata or {}).get("style_id")
    style_changed = bool(prior_style) and prior_style != style_id
    adapter = get_adapter(config, style)
    target = adapter.resolve_target(params if ctx.replay else None)
    notes: list[str] = []
    mismatch = "; it will not match" if ctx.replay else ""
    recorded_source = params.get("source")
    if isinstance(recorded_source, str) and recorded_source and recorded_source != adapter.source_id:
        was = next((s["label"] for s in list_sources() if s["id"] == recorded_source), recorded_source)
        notes.append(f"made on {was}, re-rendered on {adapter.label}{mismatch}")
    if not target.supports_seed and ctx.replay:
        notes.append("this provider takes no seed: a fresh render of the same prompt, billed as one, not the original image")
    recorded_references = _recorded_references(params)
    replay_targets = replay_slots(target, recorded_references)
    if recorded_references and not replay_targets:
        references = ()
        notes.append(f"this style does not take reference images, so the original's reference was not sent{mismatch}")
    else:
        references = await refetch_references(recorded_references, slots=replay_targets)
        dropped = len(recorded_references) - len(references)
        if dropped > 0:
            notes.append(f"this style takes fewer reference images, so {dropped} of them were not sent")
    if recorded_references or references:
        params["references"] = [reference.record() for reference in references]
    resolved_seed = fold_seed(seed)
    result = await _rendered(
        adapter,
        ImageRequest(
            prompt=prompt,
            negative_prompt=negative,
            seed=resolved_seed,
            style_id=style_id,
            timeout_seconds=config["timeout_seconds"],
            references=references,
        ),
        target=target,
    )
    params.update(_render_record(result, source=adapter.source_id))
    consumption = _consumption(style, prompt, negative, result, params, source_label=adapter.label)
    if notes:
        consumption["notes"] = [*notes, *consumption.get("notes", [])]
    if style_changed:
        consumption.setdefault("notes", []).append(
            f"style changed to {style['label']}; the prompt still carries the previous style's wording"
        )
    return result.image_bytes, consumption
