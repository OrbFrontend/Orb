"""Standalone LLM scene composer and deterministic prompt assembly."""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

from ..contracts import ToolSpec
from ..toolkit import forced_tool_call
from .config import resolve_style

COMPOSE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "compose_image_prompt",
        "description": "Tag the current visible moment without choosing an art style.",
        "parameters": {
            "type": "object",
            "properties": {
                "scene": {
                    "type": "string",
                    "description": (
                        "Comma-separated booru-style tags, not sentences. Each tag is 1-3 words with no clauses; aim for "
                        "roughly 10-20 tags total. Never write proper or character names -- describe each person by visible "
                        "attributes (hair, build, clothing) instead. Cover subject count, setting, lighting, pose, "
                        "expression, and clothing. Omit incidental background objects. No art-style or quality terms."
                    ),
                },
                "avoid": {
                    "type": ["string", "null"],
                    "description": "Optional comma-separated tags for visible elements that must not appear.",
                },
            },
            "required": ["scene", "avoid"],
            "additionalProperties": False,
        },
    },
}

COMPOSE_TOOL = ToolSpec(
    name="compose_image_prompt",
    schema=COMPOSE_TOOL_SCHEMA,
    choice={"type": "function", "function": {"name": "compose_image_prompt"}},
    standalone=True,
)


def _bounded(value: Any, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip(" ,")[:limit].strip(" ,")


def _join(parts: Sequence[Any]) -> str:
    return ", ".join(part for part in (_bounded(p) for p in parts) if part)[:6_000].strip(" ,")


async def compose_scene(
    *,
    client: Any,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    anchor_text: str,
) -> tuple[str, str, str]:
    tail = [
        {
            "role": "user",
            "content": (
                "[OOC: Call compose_image_prompt for the visible moment in the assistant reply above. Use only details "
                "directly established by the conversation, prefer the most recent explicit detail, and do not invent an "
                "outfit, pose, or object when the text does not establish it.]"
            ),
        }
    ]
    args: dict = {}
    async for event in forced_tool_call(
        client=client,
        prefix=prefix,
        tail_messages=tail,
        tool_name="compose_image_prompt",
        settings=settings,
        reasoning_on=True,
        temperature=0.2,
        max_tokens=1_024,
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    scene = _bounded(args.get("scene")) or _bounded(anchor_text, 1_200)
    if not scene:
        raise ValueError("message has no visual text to compose")
    return (
        scene,
        _bounded(args.get("avoid")),
        "single_call" if args.get("scene") else "fallback_excerpt",
    )


def assemble_prompts(
    config: Mapping[str, Any],
    style_id: str,
    profile: Mapping[str, Any],
    scene: str,
    avoid: str,
) -> tuple[str, str, dict]:
    style = resolve_style(config, style_id)
    positive = _join((profile.get("appearance_prompt"), scene, style.get("prompt")))
    negative = _join((profile.get("negative_prompt"), avoid, style.get("negative_prompt")))
    return positive, negative, style
