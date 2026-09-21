"""Select image-composition skills, compose a prompt, and clean the result."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from ..toolkit import forced_tool_call
from .config import DEFAULT_PROMPT_FORMAT
from .pov import THIRD
from .prompts import OFFER_TOOLS, compose_ooc, select_skills_ooc
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


class SkillSelection(NamedTuple):
    """A validated selector result and whether its visibility answer is usable."""

    skills: tuple[dict, ...] = ()
    visible_subjects: tuple[str, ...] = ()
    valid: bool = False


async def _forced_args(*, client, model_name, prefix, tail, tool_name, settings, reasoning_on) -> dict:
    logger.info("[image_gen] %s tail:\n%s", tool_name, "\n--\n".join(m["content"] for m in tail))
    args: dict = {}
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
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    logger.info("[image_gen] %s returned: %s", tool_name, args)
    return args


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
        args = await _forced_args(
            client=client,
            model_name=model_name,
            prefix=prefix,
            tail=[{"role": "user", "content": select_skills_ooc(pov, _sheets(subjects), catalog)}],
            tool_name="read_image_skills",
            settings=settings,
            reasoning_on=reasoning_on,
        )
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
) -> tuple[str, str, str]:
    """Compose scene text as ``(scene, avoid, mode)``."""
    sheets = _sheets(subjects)
    args = await _forced_args(
        client=client,
        model_name=model_name,
        prefix=prefix,
        tail=[
            {
                "role": "user",
                "content": compose_ooc(
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
                ),
            }
        ],
        tool_name="compose_image_prompt",
        settings=settings,
        reasoning_on=reasoning_on,
    )

    scene = clean_scene(bounded(args.get("scene")), prompt_format=prompt_format, pov=pov)
    if not scene:
        raise ValueError("couldn't compose an image prompt for this message")
    names = visible_subjects if visible_subjects is not None else args.get("visible_subjects")
    visible = _matching_subjects(sheets, names, "composition") if isinstance(names, (list, tuple)) else []
    scene = inject_profile_appearance(scene, visible, prompt_format)
    return scene, bounded(args.get("avoid")), "scene_skills" if visible_subjects is not None else "single_call"


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
