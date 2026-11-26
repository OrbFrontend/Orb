"""Deterministic Scene Direction projection and rendering."""

from __future__ import annotations

from collections.abc import Collection, Mapping, MutableMapping, Sequence
from typing import Any

from ..core import REPLY_EDIT_FIELD_TYPES, STATE_FIELD_TYPE, resolve_stored_random

# The heading the Director's other readers see; the Writer reads the guidance under its prompt layout's Scene guidance text.
SCENE_GUIDANCE_HEADING = "**Scene Guidance**"


def resolve_mood_fragment_randoms(
    mood_fragments: Sequence[Mapping[str, Any]], renderable_ids: Collection[str], choices: MutableMapping[str, str]
) -> list[Mapping[str, Any]]:
    """Resolve stored random macros in renderable mood prompt fields."""
    resolved: list[Mapping[str, Any]] = []
    for fragment in mood_fragments:
        if fragment["id"] in renderable_ids:
            prompt_text, negative_prompt = resolve_stored_random(
                [fragment.get("prompt_text", ""), fragment.get("negative_prompt", "")], choices, f"mood:{fragment['id']}"
            )
            fragment = {**fragment, "prompt_text": prompt_text, "negative_prompt": negative_prompt}
        resolved.append(fragment)
    return resolved


def compute_style_injection_block(
    active_moods: list[str],
    prior_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    direct_scene_enabled: bool,
    extra_fields: dict | None = None,
) -> str:
    """Compute the Scene Guidance body from Director outputs, without its heading.

    State fragments render separately, in the recipient's current-state block
    (``prompting.fragment_state``), so they never enter Scene Guidance.
    """
    if extra_fields is None:
        extra_fields = {}

    if direct_scene_enabled:
        injection_moods = active_moods
        injection_extra = extra_fields
    else:
        injection_moods = []
        injection_extra = {}

    deactivated = (
        [fragment for fragment in mood_fragments if fragment["id"] in (set(prior_moods) - set(injection_moods))]
        if direct_scene_enabled
        else []
    )
    active = [fragment for fragment in mood_fragments if fragment["id"] in injection_moods]

    if not (active or deactivated or injection_extra):
        return ""
    return build_style_injection(active, deactivated, interactive_fragments, injection_extra)


def build_style_injection(
    active: Sequence[Mapping[str, Any]],
    deactivated: Sequence[Mapping[str, Any]] | None = None,
    interactive_fragments: Sequence[Mapping[str, Any]] | None = None,
    extra_fields: dict | None = None,
) -> str:
    """Render the Scene Guidance body: active mood prompts, deactivated moods' negative prompts, then field values."""
    parts: list[str] = []
    for fragment in active:
        parts.append(fragment["prompt_text"])
    for fragment in deactivated or []:
        if negative := fragment.get("negative_prompt", "").strip():
            parts.append(negative)

    for fragment in sorted(interactive_fragments or [], key=lambda item: item.get("sort_order", 0)):
        if fragment.get("field_type") in REPLY_EDIT_FIELD_TYPES or fragment.get("field_type") == STATE_FIELD_TYPE:
            continue
        value = (extra_fields or {}).get(fragment["id"])
        if not value:
            continue
        parts.append(scene_field_text(fragment, value))
    return "\n\n".join(part for part in parts if part)


def scene_field_text(fragment: Mapping[str, Any], value: Any) -> str:
    """One Director field as Scene Guidance shows it: a list under its label, anything else after it."""
    label = fragment["injection_label"]
    if fragment["field_type"] == "array" and isinstance(value, list):
        return label + ":\n" + "\n".join(f"- {item}" for item in value)
    return f"{label}: {value}"


def headed_scene_guidance(body: str) -> str:
    """*body* under the Scene Guidance heading; nothing when *body* is empty."""
    return f"{SCENE_GUIDANCE_HEADING}\n\n{body}" if body else ""
