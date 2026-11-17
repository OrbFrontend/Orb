"""Clean and normalize composed image prompts."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any, NamedTuple

from .config import DEFAULT_PROMPT_FORMAT, PROMPT_FORMATS


def normalize_prompt_format(value: str) -> str:
    return value if value in PROMPT_FORMATS else DEFAULT_PROMPT_FORMAT


# A nullable field comes back as the literal word "null" often enough that an
# unguarded read ships it into the scene and the negative prompt.
_NULLISH = frozenset(("null", "none", "nil", "n/a", "undefined", "unknown"))


def bounded(value: Any, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip(" ,")[:limit].strip(" ,")
    return "" if text.casefold() in _NULLISH else text


def join(parts: Sequence[Any]) -> str:
    return ", ".join(part for part in (bounded(p) for p in parts) if part)[:6_000].strip(" ,")


# "no humans" is booru's count of zero: the background shot leads with it, and it
# must stay ahead of the style block like any other count.
_COUNT_TOKEN = r"(?:\d+\+?\s*(?:girls?|boys?|others?)|multiple\s+(?:girls|boys|others)|no\s+humans|solo|pov)"


_COUNT_CHUNK_RE = re.compile(rf"{_COUNT_TOKEN}(?:\s+{_COUNT_TOKEN})*", re.IGNORECASE)


_PROSE_COUNT_PREFIX_RE = re.compile(rf"^(?:(?:{_COUNT_TOKEN})\b\s*[,.;:]?\s*)+", re.IGNORECASE)


# CLIP has no negation: a saved sheet's "no longer wearing X" chunk draws X.
_NEGATION_CHUNK_RE = re.compile(r"(?:no longer wearing|not wearing|without)\b", re.IGNORECASE)


# A saved appearance sheet is frontal, so on a back shot it must not carry
# face-only traits. Applied only when the analyzer flags the face hidden.
_FACE_CHUNK_RE = re.compile(
    r"\b(eyes?|eyeliner|eye ?shadow|eyelashes?|lashes|eyebrows?|mascara"
    r"|lips?|lipstick|mouth|teeth|fangs?|makeup)\b",
    re.IGNORECASE,
)


def strip_chunks(text: str, pattern: re.Pattern, *, whole: bool = True) -> str:
    """Drop comma chunks the pattern hits. `whole` matches a chunk that IS the
    pattern (count blocks); otherwise the pattern need only appear inside it."""
    hit = pattern.fullmatch if whole else pattern.search
    return ", ".join(c for c in (c.strip() for c in text.split(",")) if c and not hit(c))


def split_lead_count(scene: str) -> tuple[str, str]:
    """Peel the leading count/pov chunks off, as ``(count_lead, remainder)``.

    Booru training puts counts first, and a long appearance in front of them pushes them out of CLIP's first 77-token window.
    """
    parts = [c.strip() for c in scene.split(",") if c.strip()]
    lead = 0
    while lead < len(parts) and _COUNT_CHUNK_RE.fullmatch(parts[lead]):
        lead += 1
    return ", ".join(parts[:lead]), ", ".join(parts[lead:])


def strip_prose_count_prefix(scene: str) -> str:
    """Remove leaked booru count tags from the head of a prose prompt, where a
    prose encoder reads ``1boy`` literally rather than as metadata. Natural
    language elsewhere is untouched."""
    return _PROSE_COUNT_PREFIX_RE.sub("", scene).lstrip(" ,.;:-")


class SubjectAppearance(NamedTuple):
    """One subject's saved appearance sheet, as the injector needs it.

    The pure projection of a `subjects.Subject`: a name, the fixed tags, and whether the analyzer could see this person's face.
    Declared here rather than imported because this module reads no config and calls no model, and the composer is what turns a
    resolved subject and an analysis into one of these.
    """

    name: str
    appearance: str
    face_visible: bool = True


def inject_profile_appearance(scene: str, subjects: Sequence[SubjectAppearance], prompt_format: str) -> str:
    """Insert fixed appearance traits for visible subjects."""
    normalized_format = normalize_prompt_format(prompt_format)
    rendered: list[str] = []
    for subject in subjects:
        fixed = strip_chunks(bounded(subject.appearance), _COUNT_CHUNK_RE)
        fixed = strip_chunks(fixed, _NEGATION_CHUNK_RE, whole=False)
        if not subject.face_visible:
            fixed = strip_chunks(fixed, _FACE_CHUNK_RE, whole=False)
        if fixed:
            rendered.append(fixed if normalized_format != "prose" or fixed[-1] in ".!?" else f"{fixed}.")
    if not rendered:
        return scene
    # Seated right after the count anchor, so identity stays near the high-attention head rather than landing after the setting.
    count_lead, body = split_lead_count(scene)
    if normalized_format == "prose":
        prose_body = " ".join(part for part in (*rendered, body) if part)
        return ", ".join(part for part in (count_lead, prose_body) if part)
    return join((count_lead, *rendered, body))


def strip_count_tags(text: str) -> str:
    """Drop every booru count chunk -- "1girl", "2boys", "solo", "pov".

    Prose carries no count metadata, so a saved style block or a stale scene that
    smuggled one in must not reach a prose encoder that would read it literally.
    """
    return strip_chunks(text, _COUNT_CHUNK_RE)


def clean_scene(scene: str, *, prompt_format: str) -> str:
    """Normalize the composer's count tags without changing what the scene says; the review reads it as sent."""
    if normalize_prompt_format(prompt_format) == "prose":
        return strip_prose_count_prefix(scene)
    return re.sub(rf"\b({_COUNT_TOKEN})\.", r"\1,", scene, flags=re.IGNORECASE)
