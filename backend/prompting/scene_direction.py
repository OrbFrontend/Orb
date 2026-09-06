"""Deterministic Scene Direction projection and rendering."""

from __future__ import annotations

from collections.abc import Collection, Mapping, MutableMapping, Sequence
from typing import Any

from ..core import resolve_stored_random


def resolve_mood_fragment_randoms(
    mood_fragments: Sequence[Mapping[str, Any]],
    renderable_ids: Collection[str],
    choices: MutableMapping[str, str],
) -> list[Mapping[str, Any]]:
    """Resolve stored random macros in renderable mood prompt fields."""
    resolved: list[Mapping[str, Any]] = []
    for fragment in mood_fragments:
        if fragment["id"] in renderable_ids:
            prompt_text, negative_prompt = resolve_stored_random(
                [fragment.get("prompt_text", ""), fragment.get("negative_prompt", "")],
                choices,
                f"mood:{fragment['id']}",
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
    prior_progressive_state: dict | None = None,
) -> str:
    """Compute the Scene Direction block from Director outputs."""
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
        if direct_scene_enabled and injection_moods
        else []
    )
    active = [fragment for fragment in mood_fragments if fragment["id"] in injection_moods]

    if not (active or deactivated or injection_extra):
        return ""
    return build_style_injection(
        active,
        deactivated,
        interactive_fragments,
        injection_extra,
        prior_progressive_state,
    )


def build_style_injection(
    active: Sequence[Mapping[str, Any]],
    deactivated: Sequence[Mapping[str, Any]] | None = None,
    interactive_fragments: Sequence[Mapping[str, Any]] | None = None,
    extra_fields: dict | None = None,
    prior_progressive_state: dict | None = None,
) -> str:
    """Render the Scene Direction block for the Writer pass."""
    parts = ["**Scene Direction**"]
    for fragment in active:
        parts.append(fragment["prompt_text"])
    for fragment in deactivated or []:
        if negative := fragment.get("negative_prompt", "").strip():
            parts.append(negative)

    for fragment in sorted(interactive_fragments or [], key=lambda item: item.get("sort_order", 0)):
        value = (extra_fields or {}).get(fragment["id"])
        if not value:
            continue
        label = fragment["injection_label"]
        if fragment["field_type"] == "array" and isinstance(value, list):
            parts.append(label + ":\n" + "\n".join(f"- {item}" for item in value))
        elif fragment["field_type"] == "progressive":
            old_value = (prior_progressive_state or {}).get(fragment["id"])
            transition = f"{old_value} -> {value}" if old_value and old_value != value else str(value)
            parts.append(f"{label} ({fragment['description']}): {transition}")
        else:
            parts.append(f"{label}: {value}")
    return "\n\n".join(parts)
