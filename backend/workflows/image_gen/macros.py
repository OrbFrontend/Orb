"""Expand macros in saved image-generation text."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..toolkit import Macros
from .subjects import Subject

_STYLE_TEXT = ("prompt", "negative_prompt", "extra_instructions")
_PROFILE_TEXT = ("appearance_prompt", "negative_prompt")
_SKILL_TEXT = ("description", "instructions")


def _expanded(source: Mapping[str, Any], fields: Sequence[str], macros: Macros) -> dict:
    """Copy *source* with macros expanded in *fields*."""
    out = dict(source)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = macros.resolve_message(value)
    return out


def _for_subject(macros: Macros, name: str) -> Macros:
    """Use a subject's name for `{{char}}` while preserving other macros."""
    return Macros(user=macros.user, char=name or macros.char, seed=macros.seed, cast=macros.cast)


def expand_style(style: Mapping[str, Any], macros: Macros) -> dict:
    """Expand macros in a style's model-facing text."""
    return _expanded(style, _STYLE_TEXT, macros)


def expand_profile(profile: Mapping[str, Any], macros: Macros) -> dict:
    """Expand macros in a character appearance sheet."""
    return _expanded(profile, _PROFILE_TEXT, macros)


def expand_skills(skills: Sequence[Mapping[str, Any]], macros: Macros) -> tuple[dict, ...]:
    """Expand macros in composition-skill prose."""
    return tuple(_expanded(skill, _SKILL_TEXT, macros) for skill in skills if isinstance(skill, Mapping))


def expand_subjects(subjects: Sequence[Subject], macros: Macros) -> tuple[Subject, ...]:
    """Expand each subject's appearance sheet against its own name."""
    return tuple(
        replace(
            subject,
            profile=expand_profile(subject.profile, _for_subject(macros, subject.name)),
        )
        for subject in subjects
    )
