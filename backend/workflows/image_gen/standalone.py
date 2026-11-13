"""Description-driven pictures, independent of RP messages and their attachment lifecycle."""

from __future__ import annotations

import secrets

from ..toolkit import Macros, StandaloneCtx, StandaloneResult, get_workflow_character_state, get_workflow_config
from .composer import PrompterCallError, compose_standalone
from .config import WORKFLOW_ID, normalize_config, normalize_profile, resolve_style, style_reference_source
from .engine import ImageGenerationError, ImageRequest, get_adapter, resolve_and_generate
from .hooks import render_progress, review_image_url
from .macros import expand_profile, expand_style
from .references import plan_slots, resolve_references
from .subjects import Subject


async def standalone(ctx: StandaloneCtx) -> StandaloneResult:
    """Use the default style, one prompter call and one render, returning bytes to the host."""
    config = normalize_config(await get_workflow_config(WORKFLOW_ID))
    name = str((ctx.character or {}).get("name") or "")
    macros = Macros(user=str(ctx.settings.get("user_name") or "User"), char=name, seed="")
    style = expand_style(resolve_style(config, config["default_style"]), macros)
    profile = expand_profile(
        normalize_profile(await get_workflow_character_state(ctx.character_id, WORKFLOW_ID) if ctx.character_id else None),
        macros,
    )
    subjects = (Subject("", ctx.character_id, name, profile),) if ctx.character_id else ()
    # No chat image exists here. An explicit picture fills the style's image inputs even when the style leaves them off; without
    # one, a style that enables references draws this character's likeness.
    previous = (ctx.reference.data, ctx.reference.mime, ctx.reference.origin) if ctx.reference is not None else None
    source = "previous" if previous is not None else "character" if style_reference_source(style) else ""
    style = {**style, "reference_source": source}
    adapter = get_adapter(config, style)
    target = adapter.resolve_target(None)
    slots = plan_slots(target, subjects, previous=previous)
    if ctx.reference is not None and not slots:
        raise ImageGenerationError(
            "The default image style has no reference image inputs. Choose a style or backend that accepts references."
        )
    references = await resolve_references(slots, subjects=subjects, previous=previous)
    ctx.phase("Composing the image prompt…")
    try:
        prompt, negative = await compose_standalone(
            client=ctx.agent_client,
            model_name=ctx.agent_model_name,
            settings=ctx.settings,
            description=ctx.description,
            style=style,
            profile=profile,
            subjects=subjects,
            supports_negative=target.supports_negative_prompt,
            has_references=bool(references),
            reasoning_on=bool(config.get("prompter_reasoning")),
        )
    except (PrompterCallError, ValueError) as exc:
        raise ImageGenerationError(f"Could not compose the standalone picture: {exc}") from exc
    result = await resolve_and_generate(
        adapter,
        ImageRequest(
            prompt=prompt,
            negative_prompt=negative,
            seed=secrets.randbits(64),
            style_id=style["id"],
            timeout_seconds=config["timeout_seconds"],
            references=references,
        ),
        target=target,
        progress=render_progress(ctx.phase),
    )
    # The caller's model is the Agent lane. It is shown the picture only where this workflow's own vision features are on: a
    # model without vision fails the call that carries a picture, and every later call that replays it.
    sees = config.get("prompter_reference") or config.get("refine_turns")
    preview = await review_image_url(result.image_bytes, result.mime) if sees else ""
    return StandaloneResult(result.image_bytes, result.mime, preview)
