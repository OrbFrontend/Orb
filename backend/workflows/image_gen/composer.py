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
                        "An image prompt written the way this model is prompted: booru tags and short natural-language "
                        "clauses mixed freely, comma-separated. Start with the count anchor (1girl, 1boy, 2girls, "
                        "1boy 1girl, ...). Then give EACH character their own clause -- who they are, hair, eyes, build, "
                        "clothing, pose, action, and their own expression -- keeping every attribute inside that "
                        "character's clause so it does not bleed onto the others (e.g. 'a slim woman with long red hair "
                        "and red eyes in a silk dress, holding a book, teary-eyed'); keep each comma-separated chunk "
                        "self-contained so meaning does not leak across boundaries. For a first-person point-of-view "
                        "scene, add the pov tag and do not draw the camera character (hands at most), leaving only the "
                        "others as subjects; add looking at viewer when someone faces the camera. Add the interaction "
                        "between them, then shared setting, lighting, and framing last. Use as many clauses as the moment "
                        "needs; do not compress to single-word tags. No art-style or quality terms."
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

# Structured scene, used only when `scene_analysis` is on. The point is the outfit
# delta and per-character spatial fields: a flat `characters` array (one object per
# person) keeps rendering trivial and sidesteps the name-matching a parallel-array
# shape needs. Every field required; optionals are nullable, matching the compose
# schema's strict style.
ANALYZE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "analyze_scene",
        "description": (
            "Extract the structured scene from the conversation: the viewpoint, who is visible, each one's outfit "
            "as a delta from their default, and where each stands relative to anchors and to each other."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "viewpoint": {
                    "type": "string",
                    "enum": ["first_person", "third_person"],
                    "description": (
                        "first_person when the moment is narrated through a character's eyes (usually the user, 'you') "
                        "-- that character is the camera and is NOT listed below. third_person otherwise."
                    ),
                },
                "characters": {
                    "type": "array",
                    "description": "One entry per character actually visible in frame. Excludes the camera character in first_person.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Short label for this character."},
                            "appearance": {
                                "type": "string",
                                "description": (
                                    "Visible fixed traits (hair, eyes, build). Leave empty for the main "
                                    "character, whose default appearance is supplied separately."
                                ),
                            },
                            "outfit_added": {
                                "type": ["string", "null"],
                                "description": "Comma-separated articles worn in addition to, or in place of, the default outfit.",
                            },
                            "outfit_removed": {
                                "type": ["string", "null"],
                                "description": "Comma-separated default articles that are absent in this moment.",
                            },
                            "position": {
                                "type": ["string", "null"],
                                "description": "Where they stand relative to anchors and to the other characters (left, right, behind, etc.).",
                            },
                            "pose": {"type": ["string", "null"], "description": "Current pose."},
                            "action": {"type": ["string", "null"], "description": "What they are doing in this moment."},
                        },
                        "required": ["name", "appearance", "outfit_added", "outfit_removed", "position", "pose", "action"],
                        "additionalProperties": False,
                    },
                },
                "anchors": {
                    "type": ["string", "null"],
                    "description": "Comma-separated setting objects the characters are positioned against.",
                },
                "setting": {"type": ["string", "null"], "description": "Location, time of day, and lighting."},
            },
            "required": ["viewpoint", "characters", "anchors", "setting"],
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

ANALYZE_TOOL = ToolSpec(
    name="analyze_scene",
    schema=ANALYZE_TOOL_SCHEMA,
    choice={"type": "function", "function": {"name": "analyze_scene"}},
    standalone=True,
)

# Format (count anchor, per-character binding, comma isolation, POV) lives once, in
# the compose schema description both paths already see. These carry only the task:
# where the facts come from. Single-call extracts and infers POV itself; the
# analysis path is handed both, so it only formats.
_COMPOSE_OOC = (
    "[OOC: Call compose_image_prompt for the visible moment in the assistant reply above, following the tool's format. "
    "Use only details established by the conversation, and prefer the most recent explicit detail. Decide the point of view "
    "from the narration voice -- narration through a character's eyes (usually the user, 'you') is first-person.]"
)

_COMPOSE_FORMAT = (
    "[OOC: Call compose_image_prompt depicting exactly the structured scene below and nothing beyond it, following the "
    "tool's format and the scene's viewpoint line.]"
)

_ANALYZE_OOC = (
    "[OOC: Call analyze_scene for the visible moment in the assistant reply above. Use ONLY what the history directly "
    "establishes; for every attribute take the most recent explicit statement, and where nothing changed leave it at the "
    "character's default. Report each present character's outfit as a delta from their default. Do not infer outfits, poses, "
    "or positions from genre convention. Decide the viewpoint from the narration voice, and in first_person leave the camera "
    "character out of the character list; list only characters actually visible in frame.]"
)


def _bounded(value: Any, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip(" ,")[:limit].strip(" ,")


def _join(parts: Sequence[Any]) -> str:
    return ", ".join(part for part in (_bounded(p) for p in parts) if part)[:6_000].strip(" ,")


async def _forced_args(*, client, prefix, tail, tool_name, settings, max_tokens) -> dict:
    args: dict = {}
    async for event in forced_tool_call(
        client=client,
        prefix=prefix,
        tail_messages=tail,
        tool_name=tool_name,
        settings=settings,
        reasoning_on=True,
        temperature=0.2,
        max_tokens=max_tokens,
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    return args


def _render_scene(scene: Any) -> str:
    """Structured analyze_scene args -> compact text for the composition call.

    Tolerant of missing/malformed fields: any absent character or section is
    dropped, so a partial scene from the model still yields usable text.
    """
    if not isinstance(scene, Mapping):
        return ""
    lines: list[str] = []
    if _bounded(scene.get("viewpoint")) == "first_person":
        lines.append("viewpoint: first-person POV (pov) -- the camera character is not drawn, hands at most")
    for ch in scene.get("characters") or []:
        if not isinstance(ch, Mapping):
            continue
        name = _bounded(ch.get("name")) or "character"
        bits: list[str] = []
        appearance = _bounded(ch.get("appearance"))
        if appearance:
            bits.append(appearance)
        added = _bounded(ch.get("outfit_added"))
        if added:
            bits.append(f"wearing {added}")
        removed = _bounded(ch.get("outfit_removed"))
        if removed:
            bits.append(f"no longer wearing {removed}")
        for key in ("position", "pose", "action"):
            value = _bounded(ch.get(key))
            if value:
                bits.append(value)
        if bits:
            lines.append(f"{name}: " + ", ".join(bits))
    tail = _join((scene.get("setting"), scene.get("anchors")))
    if tail:
        lines.append(f"setting: {tail}")
    return "\n".join(lines)


async def compose_scene(
    *,
    client: Any,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    anchor_text: str,
    scene_analysis: bool = False,
    appearance: str = "",
) -> tuple[str, str, str]:
    analysis_block = ""
    if scene_analysis:
        instr = _ANALYZE_OOC
        if appearance.strip():
            instr += "\n\nMain character's default appearance and outfit:\n" + appearance.strip()
        analysis = await _forced_args(
            client=client,
            prefix=prefix,
            tail=[{"role": "user", "content": instr}],
            tool_name="analyze_scene",
            settings=settings,
            max_tokens=2_048,
        )
        analysis_block = _render_scene(analysis)

    if analysis_block:
        # Format-only framing, then the scene as the final message where attention
        # is strongest: the composer renders exactly this instead of re-deriving it.
        tail = [
            {"role": "user", "content": _COMPOSE_FORMAT},
            {"role": "user", "content": "Structured scene extracted from the conversation:\n\n" + analysis_block},
        ]
    else:
        tail = [{"role": "user", "content": _COMPOSE_OOC}]
    args = await _forced_args(
        client=client,
        prefix=prefix,
        tail=tail,
        tool_name="compose_image_prompt",
        settings=settings,
        max_tokens=1_024,
    )

    scene = _bounded(args.get("scene")) or _bounded(anchor_text, 1_200)
    if not scene:
        raise ValueError("message has no visual text to compose")
    if not args.get("scene"):
        mode = "fallback_excerpt"
    elif analysis_block:
        mode = "scene_analysis"
    else:
        mode = "single_call"
    return scene, _bounded(args.get("avoid")), mode


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
