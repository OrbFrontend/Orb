"""Expand user macros in saved image-gen text.

Styles, character appearance sheets, and composition skills are authored once and
reused in every chat, so `{{char}}` in them can only mean something at render time.
That is the whole reason this lives here rather than at the persistence boundary:
one saved style reads correctly everywhere, and an edit-model instruction such as
"`<image1>` is `{{char}}`" names whoever this render is a picture of.

Expansion happens exactly once per render, on the way out of `resolve_style` and
`subjects.resolve`. Once matters: the composer is *told* what the image model will
receive outside its tool output, so a `{{random}}` resolved a second time would
have it write around wording the render never sends.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from ..toolkit import Macros
from .subjects import Subject

# Model-facing style text. `prompt_format`, ids, and every connection field are
# machine values, not prose, and stay literal.
_STYLE_TEXT = ("prompt", "negative_prompt", "extra_instructions")

_PROFILE_TEXT = ("appearance_prompt", "negative_prompt")

# `id` and `label` stay literal: the selector answers with an id and the render
# discloses the label next to the image, so both are identifiers rather than prose.
_SKILL_TEXT = ("description", "instructions")


def _expanded(source: Mapping[str, Any], fields: Sequence[str], macros: Macros) -> dict:
    """Copy *source* with macros expanded in *fields*."""
    out = dict(source)
    for field in fields:
        value = out.get(field)
        if isinstance(value, str) and value:
            out[field] = macros.resolve_message(value)
    return out


def for_subject(macros: Macros, name: str) -> Macros:
    """Scope `{{char}}` to one subject.

    An appearance sheet is saved on that character's own card, so `{{char}}` in it
    means that character -- not the scene title a group chat otherwise resolves
    `{{char}}` to. The same rule the group prompt applies to card text it moves into
    a shared body. `{{user}}`, `{{cast}}`, and the seed ride along untouched.
    """
    return Macros(user=macros.user, char=name or macros.char, seed=macros.seed, cast=macros.cast)


def expand_style(style: Mapping[str, Any], macros: Macros) -> dict:
    """Expand macros in one style's prompt blocks and prompter guidance."""
    return _expanded(style, _STYLE_TEXT, macros)


def expand_profile(profile: Mapping[str, Any], macros: Macros) -> dict:
    """Expand macros in one character's appearance sheet."""
    return _expanded(profile, _PROFILE_TEXT, macros)


def expand_skills(skills: Sequence[Mapping[str, Any]], macros: Macros) -> tuple[dict, ...]:
    """Expand macros in the composition-skill library, in library order."""
    return tuple(_expanded(skill, _SKILL_TEXT, macros) for skill in skills if isinstance(skill, Mapping))


def expand_subjects(subjects: Sequence[Subject], macros: Macros) -> tuple[Subject, ...]:
    """Expand every subject's appearance sheet, each against its own name."""
    return tuple(
        replace(subject, profile=expand_profile(subject.profile, for_subject(macros, subject.name))) for subject in subjects
    )
