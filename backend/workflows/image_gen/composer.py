"""Select image-composition skills, compose a prompt, and clean the result."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from ..toolkit import forced_tool_call
from .config import DEFAULT_PROMPT_FORMAT
from .pov import THIRD
from .prompts import (
    OFFER_TOOLS,
    compose_ooc,
    refine_ooc,
    render_result,
    select_skills_ooc,
)
from .scrub import (
    SubjectAppearance,
    bounded,
    clean_scene,
    inject_profile_appearance,
    join,
    normalize_prompt_format,
    split_lead_count,
    strip_count_tags,
    strip_prose_count_prefix,
)
from .subjects import Subject

logger = logging.getLogger(__name__)


class PrompterCallError(RuntimeError):
    """The provider refused a prompter call that was not allowed to degrade.

    Kept apart from the composer's own `ValueError`, which says the model answered
    but wrote nothing usable.
    """


class SkillSelection(NamedTuple):
    """A validated selector result and whether its visibility answer is usable."""

    skills: tuple[dict, ...] = ()
    visible_subjects: tuple[str, ...] = ()
    valid: bool = False


@dataclass
class RefineThread:
    """The compose call and every review after it, kept so each review sees the rest.

    `messages` extends the shared prefix: the compose tail, then per render the
    replayed call, its tool result, and the image under review. `call_id` names the
    call the next render answers. `prompter_reference` says the compose tail carried
    the chat's earlier picture, so each review checks against it too.
    """

    messages: list[dict] = field(default_factory=list)
    visible: list[SubjectAppearance] = field(default_factory=list)
    call_id: str = ""
    prompter_reference: bool = False


@dataclass
class Revision:
    """One review of a render: the critique, and the revised prompt unless accepted.

    `reseed` asks for the next render to be drawn from a new seed.
    """

    critique: str
    done: bool
    scene: str = ""
    avoid: str = ""
    reseed: bool = False


def _call_id(render: int) -> str:
    """The id of the call made after `render` renders: 0 is compose, n the review of render n.

    Nine alphanumerics, the strictest id shape a provider enforces (Mistral), so the
    replayed thread is valid wherever it is sent.
    """
    return f"imgcall{render:02d}"


def _logged_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    parts = content if isinstance(content, list) else []
    return " ".join(part["text"] if part.get("type") == "text" else "<image>" for part in parts if isinstance(part, Mapping))


async def _forced_result(
    *,
    client,
    model_name,
    prefix,
    tail,
    tool_name,
    settings,
    reasoning_on,
    call_id: str | None = None,
    raise_errors: bool = False,
) -> dict:
    """The forced call's result event: `args`, plus `replay` when `call_id` asked for it.

    `raise_errors` raises the provider's error as a `PrompterCallError` instead of
    answering with empty arguments.
    """
    logger.info("[image_gen] %s tail:\n%s", tool_name, _logged_text(tail[-1]) if tail else "")
    result: dict = {"args": {}}
    try:
        async for event in forced_tool_call(
            client=client,
            prefix=prefix,
            tail_messages=tail,
            tool_name=tool_name,
            settings=settings,
            model_name=model_name,
            reasoning_on=reasoning_on,
            temperature=0.2,
            offer_tools=OFFER_TOOLS,
            call_id=call_id,
            raise_errors=raise_errors,
        ):
            if event.get("type") == "result" and isinstance(event.get("args"), dict):
                result = event
    except Exception as exc:
        if not raise_errors:
            raise
        raise PrompterCallError(str(exc) or type(exc).__name__) from exc
    logged_args = {key: value for key, value in result["args"].items() if key != "critique"}
    logger.info("[image_gen] %s returned: %s", tool_name, logged_args)
    return result


def enabled_scene_skills(skills: Sequence[Mapping[str, Any]]) -> tuple[dict, ...]:
    """Return usable entries in library order."""
    return tuple(dict(skill) for skill in skills if skill.get("enabled") is True and bounded(skill.get("instructions"), 4_000))


def _sheets(subjects: Sequence[Subject]) -> list[SubjectAppearance]:
    return [
        SubjectAppearance(name=subject.name, appearance=str(subject.profile.get("appearance_prompt") or ""))
        for subject in subjects
    ]


async def read_image_skills(
    *,
    client: Any,
    model_name: str,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    skills: Sequence[Mapping[str, Any]],
    pov: str = THIRD,
    reasoning_on: bool = False,
    subjects: Sequence[Subject] = (),
) -> SkillSelection:
    """Select up to four skills; degrade every selector failure to no selection."""
    catalog = enabled_scene_skills(skills)
    if not catalog:
        return SkillSelection()
    try:
        result = await _forced_result(
            client=client,
            model_name=model_name,
            prefix=prefix,
            tail=[{"role": "user", "content": select_skills_ooc(pov, _sheets(subjects), catalog)}],
            tool_name="read_image_skills",
            settings=settings,
            reasoning_on=reasoning_on,
        )
        args = result["args"]
    except Exception:
        logger.warning("[image_gen] composition-skill selection failed; composing without skills", exc_info=True)
        return SkillSelection()

    requested = args.get("skill_ids")
    visible = args.get("visible_subjects")
    if (
        not isinstance(requested, list)
        or not all(isinstance(value, str) for value in requested)
        or not isinstance(visible, list)
        or not all(isinstance(value, str) for value in visible)
    ):
        logger.info("[image_gen] malformed composition-skill result; composing without skills")
        return SkillSelection()

    known = {skill["id"]: skill for skill in catalog}
    selected_ids: set[str] = set()
    for value in requested:
        if value in known:
            selected_ids.add(value)
        if len(selected_ids) >= 4:
            break
    selected = tuple(skill for skill in catalog if skill["id"] in selected_ids)
    visible_names = {bounded(name, 200).casefold() for name in visible if bounded(name, 200)}
    selected_subjects = tuple(subject.name for subject in subjects if bounded(subject.name, 200).casefold() in visible_names)
    return SkillSelection(selected, selected_subjects, True)


def _matching_subjects(subjects: Sequence[Any], names: Sequence[str], where: str) -> list[Any]:
    listed = {bounded(name, 200).casefold() for name in names if bounded(name, 200)}
    matched = [subject for subject in subjects if bounded(subject.name, 200).casefold() in listed]
    matched_names = {bounded(subject.name, 200).casefold() for subject in matched}
    missing = [name for name in names if bounded(name, 200).casefold() not in matched_names]
    if missing:
        logger.info("[image_gen] %s ignored unknown subject names: %s", where, ", ".join(repr(name) for name in missing))
    return matched


def addressable_subjects(subjects: Sequence[Subject], visible_subjects: Sequence[str] | None) -> tuple[Subject, ...]:
    """Return reference candidates, using valid selector visibility when present."""
    if visible_subjects is None:
        return tuple(subjects)
    return tuple(_matching_subjects(subjects, visible_subjects, "skill selection"))


async def compose_scene(
    *,
    client: Any,
    model_name: str,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    prompt_format: str = DEFAULT_PROMPT_FORMAT,
    pov: str = THIRD,
    reasoning_on: bool = False,
    subjects: Sequence[Subject] = (),
    selected_skills: Sequence[dict] = (),
    visible_subjects: Sequence[str] | None = None,
    extra_instructions: str = "",
    supports_negative: bool = True,
    has_references: bool = False,
    referenced_subjects: Sequence[tuple[int, str]] = (),
    style_prompt: str = "",
    style_negative_prompt: str = "",
    profile_negative_prompt: str = "",
    thread: RefineThread | None = None,
    prompter_reference_url: str = "",
    prompter_reference_sent: bool = False,
    prompter_reference_prompts: tuple[str, str] = ("", ""),
) -> tuple[str, str, str]:
    """Compose scene text as ``(scene, avoid, mode)``.

    A `thread` is filled with the call as the model made it, so a review can follow.
    `prompter_reference_url` is the chat's earlier picture, sent ahead of the request
    in the same shape a review sends its render; `prompter_reference_sent` says the
    image model receives that picture as well, and `prompter_reference_prompts` is
    the prompt pair it was rendered from. A provider that rejects the image raises
    rather than composing blind.
    """
    sheets = _sheets(subjects)
    request = compose_ooc(
        prompt_format,
        pov,
        subjects=sheets,
        selected_skills=selected_skills,
        extra_instructions=extra_instructions,
        supports_negative=supports_negative,
        has_references=has_references,
        referenced_subjects=referenced_subjects,
        style_prompt=style_prompt,
        style_negative_prompt=style_negative_prompt,
        profile_negative_prompt=profile_negative_prompt,
        prompter_reference=bool(prompter_reference_url),
        prompter_reference_sent=prompter_reference_sent,
        prompter_reference_prompts=prompter_reference_prompts,
    )
    content: str | list[dict] = (
        [
            {"type": "image_url", "image_url": {"url": prompter_reference_url}},
            {"type": "text", "text": request},
        ]
        if prompter_reference_url
        else request
    )
    tail = [{"role": "user", "content": content}]
    result = await _forced_result(
        client=client,
        model_name=model_name,
        prefix=prefix,
        tail=tail,
        tool_name="compose_image_prompt",
        settings=settings,
        reasoning_on=reasoning_on,
        call_id=_call_id(0) if thread is not None else None,
        raise_errors=bool(prompter_reference_url),
    )
    args = result["args"]

    scene = clean_scene(bounded(args.get("scene")), prompt_format=prompt_format, pov=pov)
    if not scene:
        raise ValueError("couldn't compose an image prompt for this message")
    names = visible_subjects if visible_subjects is not None else args.get("visible_subjects")
    visible = _matching_subjects(sheets, names, "composition") if isinstance(names, (list, tuple)) else []
    if thread is not None and isinstance(result.get("replay"), Mapping):
        thread.messages = [*tail, dict(result["replay"])]
        thread.visible = visible
        thread.call_id = _call_id(0)
        thread.prompter_reference = bool(prompter_reference_url)
    scene = inject_profile_appearance(scene, visible, prompt_format)
    return scene, bounded(args.get("avoid")), "scene_skills" if visible_subjects is not None else "single_call"


async def refine_scene(
    *,
    client: Any,
    model_name: str,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    thread: RefineThread,
    image_url: str,
    render: int,
    turns_left: int,
    prompt_format: str = DEFAULT_PROMPT_FORMAT,
    pov: str = THIRD,
    reasoning_on: bool = False,
    supports_negative: bool = True,
    supports_seed: bool = True,
    reseeded: bool = False,
) -> Revision | None:
    """Show the model its last render and take its review, or ``None`` when it gave none.

    A provider error raises: a model that cannot read the render has no review to give.

    The render answers the thread's open call, and the review's own call is kept on
    the thread, so the next review sees every earlier image and every earlier prompt.
    `reseeded` says the render was drawn from a new seed, so the model can tell a
    change the seed made from one its prompt made.
    """
    if not thread.call_id:
        return None
    review_request = refine_ooc(
        render,
        turns_left,
        supports_negative=supports_negative,
        supports_seed=supports_seed,
        prompter_reference=thread.prompter_reference,
    )
    turn = [
        {"role": "tool", "tool_call_id": thread.call_id, "content": render_result(render, reseeded=reseeded)},
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": image_url}},
                {"type": "text", "text": review_request},
            ],
        },
    ]
    call_id = _call_id(render)
    result = await _forced_result(
        client=client,
        model_name=model_name,
        prefix=prefix,
        tail=[*thread.messages, *turn],
        tool_name="refine_image_prompt",
        settings=settings,
        reasoning_on=reasoning_on,
        call_id=call_id,
        raise_errors=True,
    )
    args, replay = result["args"], result.get("replay")
    if not isinstance(args.get("done"), bool) or not isinstance(replay, Mapping):
        logger.info("[image_gen] review of render %d came back unusable; keeping it", render)
        return None
    thread.messages = [*thread.messages, *turn, dict(replay)]
    thread.call_id = call_id
    critique = bounded(args.get("critique"))
    if args["done"]:
        return Revision(critique, True)
    scene = clean_scene(bounded(args.get("scene")), prompt_format=prompt_format, pov=pov)
    if not scene:
        logger.info("[image_gen] review of render %d asked for changes but wrote no prompt; keeping it", render)
        return Revision(critique, False)
    return Revision(
        critique,
        False,
        inject_profile_appearance(scene, thread.visible, prompt_format),
        bounded(args.get("avoid")),
        reseed=supports_seed and args.get("reseed") is True,
    )


def assemble_prompts(
    style: Mapping[str, Any],
    profile: Mapping[str, Any],
    scene: str,
    avoid: str,
) -> tuple[str, str]:
    """Join resolved style, character, and scene text into a prompt pair."""
    prompt_format = normalize_prompt_format(str(style.get("prompt_format") or ""))
    if prompt_format == "prose":
        scene_body = strip_prose_count_prefix(scene)
        style_prompt = strip_count_tags(bounded(style.get("prompt")))
        positive = strip_count_tags(join((style_prompt, scene_body)))
    else:
        count_lead, scene_body = split_lead_count(scene)
        positive = join((count_lead, style.get("prompt"), scene_body))
    negative = join((profile.get("negative_prompt"), avoid, style.get("negative_prompt")))
    return positive, negative
