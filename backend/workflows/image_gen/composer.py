"""Standalone LLM scene composer and deterministic prompt assembly."""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Sequence

from ..contracts import ToolSpec
from ..toolkit import forced_tool_call
from .config import resolve_style

logger = logging.getLogger(__name__)

# The scene format rides the OOC tail messages, not this schema: text mode never
# renders tool schemas into the prompt (the forced call is grammar-only), so any
# instruction living in a description is invisible there. The tail sits after the
# shared conversation prefix, so carrying it per-call costs no KV reuse.
#
# Written in ASD-STE100 Simplified Technical English: short sentences, one
# instruction each, imperative mood, no synonyms. A small agent model (Gemma E4B
# locally) follows plain instructions more reliably than dense prose.
#
# Two variants. The FULL guide is for the single-call path, where the compose
# model owns everything -- count anchor, viewpoint, and what to negate. The
# STRUCTURED guide is for the analysis path, where the analyzer and Python
# already own counts, viewpoint, and negatives, so the compose model only turns
# the given scene into tags; telling it to redo that work would only dilute the
# instructions that matter (Python overwrites the counts either way).
_SCENE_FORMAT = (
    "Write the image prompt as booru tags and short natural-language clauses. Separate all items with commas. "
    "Start with the count anchor. The count anchor gives the number of persons. "
    "Examples: 1girl. 1boy. 2girls. 1boy, 1girl. "
    "Give each character one clause. In each clause put that character's identity, hair, eyes, build, clothing, pose, "
    "action, and expression. Keep each character's attributes inside that character's clause. Do not move an attribute "
    "to another character. Keep each comma item complete on its own. "
    "Repeat the most important attributes of a character. Repeat them inside that same character's clause. Do not "
    "repeat them in another character's clause. "
    "For a first-person view, add the pov tag. Do not draw the viewer character. Draw the viewer character's hands "
    "only. Do not count the viewer character in the count anchor. Example: if you look at one girl, write '1girl, "
    "solo, pov', not '1boy, 1girl'. "
    "Describe only the things that you can see. "
    "Add the interaction between the characters. Then add the setting, the lighting, and the framing last. "
    "Use as many clauses as the moment needs. Do not reduce the prompt to single words. "
    "Do not add art-style words or quality words. Do not use names. "
    "In avoid, put each thing that is not visible. Example: put 'looking at viewer' if the character looks away."
)

# Formatting-only guide for the analysis path. No count, viewpoint, or avoid
# instructions: the analyzer supplies those and Python enforces them.
_SCENE_FORMAT_STRUCTURED = (
    "Show exactly the structured scene below. Do not add anything that the scene does not state. "
    "Write the image prompt as booru tags and short natural-language clauses. Separate all items with commas. "
    "Give each character one clause. In each clause put that character's traits, clothing, pose, action, and "
    "expression. Keep each character's attributes inside that character's clause. Do not move an attribute to another "
    "character. Keep each comma item complete on its own. "
    "Repeat the most important attributes of a character. Repeat them inside that same character's clause only. "
    "Add the interaction between the characters. Then add the setting, the lighting, and the framing last. "
    "Do not add count words such as 1girl or 1boy. The system already adds the count words. "
    "Do not add art-style words or quality words. Do not use names. "
    "Leave avoid empty. The system already adds the items to avoid."
)

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
                    "description": "The image prompt: booru tags and short natural-language clauses, comma-separated, per the format given in the request; can have as many items as needed.",
                },
                "avoid": {
                    "type": ["string", "null"],
                    "description": "Optional comma-separated tags for non-visible elements that must not appear.",
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
                        "-- that character is the viewer and is NOT listed below. third_person otherwise."
                    ),
                },
                "characters": {
                    "type": "array",
                    "description": "One entry per character actually visible in frame. Excludes the viewer character in first_person.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string", "description": "Short label for this character."},
                            "sex": {
                                "type": "string",
                                "enum": ["girl", "boy", "other"],
                                "description": "Count category for this character (drives the 1girl/1boy count tags).",
                            },
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
                        "required": [
                            "name",
                            "sex",
                            "appearance",
                            "outfit_added",
                            "outfit_removed",
                            "position",
                            "pose",
                            "action",
                        ],
                        "additionalProperties": False,
                    },
                },
                "anchors": {
                    "type": ["string", "null"],
                    "description": "Comma-separated setting objects the characters are positioned against.",
                },
                "setting": {"type": ["string", "null"], "description": "Location, time of day, and lighting."},
                "hidden": {
                    "type": ["string", "null"],
                    "description": (
                        "Comma-separated elements present in the moment but NOT visible -- a face turned away, an "
                        "occluded or cropped body part, a character no longer in scene, etc. -- that the image gen model should not render. Feeds "
                        "the negative prompt."
                    ),
                },
            },
            "required": ["viewpoint", "characters", "anchors", "setting", "hidden"],
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

# Each OOC carries the format guide plus where the facts come from. Single-call
# extracts and infers POV itself; the analysis path is handed both, so it only
# formats. The guide is repeated per-call rather than living in the schema or
# the prefix: tails are the one place every transport shows the model and the
# one place that never perturbs the shared prefix KV.
_COMPOSE_OOC = (
    "[OOC: Call compose_image_prompt for the visible moment in the assistant reply above. " + _SCENE_FORMAT + " "
    "Use only the details that the conversation establishes. If a detail changed, use the most recent statement. "
    "Decide the point of view from the narration voice. Narration through a character's eyes, usually the user "
    "('you'), is first-person.]"
)

_COMPOSE_FORMAT = (
    "[OOC: Call compose_image_prompt for the structured scene below. Follow the scene's viewpoint line. "
    + _SCENE_FORMAT_STRUCTURED
    + "]"
)

_ANALYZE_OOC = (
    "[OOC: Call analyze_scene for the visible moment in the assistant reply above. Use only what the history "
    "establishes directly. For each attribute, use the most recent statement. If nothing changed, keep the "
    "character's default. "
    "Report each character's sex (girl, boy, or other). Report each character's outfit as a change from the default. "
    "Put added or replaced articles in outfit_added. Put absent default articles in outfit_removed. "
    "Leave appearance empty for the main character. The system supplies the main character's default look. For each "
    "other character, give the visible fixed traits (hair, eyes, build). "
    "Do not infer outfits, poses, or positions from genre convention. Include only what is visible in this moment. "
    "Omit anything that is off-frame, implied, or assumed. "
    "In hidden, put each thing that is present but not visible. Examples: a face turned away, a body part that is "
    "occluded or cropped. A tag checkpoint can draw these by mistake, so the system negates them. "
    "Decide the viewpoint from the narration voice. For first_person, do not put the viewer character in the "
    "character list. List only the characters that are visible in frame.]"
)


def _reasoning_on(settings: Mapping[str, Any]) -> bool:
    """Reasoning mode for the off-turn analyze/compose calls, inherited from the editor.

    On a provider that keeps separate KV caches for thinking-on and thinking-off
    (the reasoning fork in docs/architecture/kv-cache.md §9), this off-turn call
    reuses the cached conversation prefix only if it lands in the same lane as a
    pipeline pass. The shipped default runs every pass thinking-off, so they share
    one lane and the choice is moot; it only bites when a user diverges the passes
    (e.g. enables director reasoning while the writer/editor stay off) — and there
    we want the *editor's* mode, not the director's:

    - The editor shares the writer's thinking-off lane, which every turn is warmed
      LAST and is the ONLY lane that has already seen this turn's user message and
      the assistant reply the image is composed from (the writer streamed the draft
      into it). A thinking-on director rides a separate lane that stops at the history
      before this turn, never touching the anchor reply. So tracking the editor reuses
      at least as much of the prefix as tracking the director, and strictly more
      whenever the writer's trailing injection is small.
    - Tracking the editor keeps this call on the writer/editor lane whatever the
      director is set to, rather than forking onto a lane nothing else warms.

    Absent/malformed config degrades to off (the writer/editor default), never raising.
    """
    passes = settings.get("reasoning_enabled_passes")
    return bool(passes.get("editor", False)) if isinstance(passes, Mapping) else False


def _bounded(value: Any, limit: int = 2_000) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip(" ,")[:limit].strip(" ,")


def _join(parts: Sequence[Any]) -> str:
    return ", ".join(part for part in (_bounded(p) for p in parts) if part)[:6_000].strip(" ,")


# The workflow's own tools blob, this pass's answer to the pipeline's base.tools:
# both off-turn calls ship these two schemas and force one via tool_choice, the
# same shape every core pass uses. A chat model needs the actual tool to call it
# reliably -- forcing via response_format with tools=None is unreliable (Gemma) or
# rejected outright (DeepSeek). Order is fixed regardless of which is forced, so
# analyze and compose are byte-identical and reuse each other's cached prefix.
# Kept out of enabled_schemas (standalone): never leaks into the pipeline's set.
_OFFER_TOOLS = ("analyze_scene", "compose_image_prompt")


async def _forced_args(*, client, prefix, tail, tool_name, settings, max_tokens, reasoning_on) -> dict:
    # Debug: the per-call instruction actually sent (the tail; the prefix is the
    # conversation itself). Set this logger to WARNING to silence.
    logger.info("[image_gen] %s tail:\n%s", tool_name, "\n--\n".join(m["content"] for m in tail))
    args: dict = {}
    async for event in forced_tool_call(
        client=client,
        prefix=prefix,
        tail_messages=tail,
        tool_name=tool_name,
        settings=settings,
        # Inherit the editor's reasoning mode so both off-turn calls ride the
        # writer/editor KV lane on a reasoning-forking provider (see _reasoning_on).
        reasoning_on=reasoning_on,
        temperature=0.2,
        max_tokens=max_tokens,
        # Ship the real tools and force via tool_choice (the pipeline pattern),
        # not tools_in_prompt=False -- that nulls tools and forces via
        # response_format, which most chat providers don't honor. In text mode
        # the schemas still never render (grammar-only), so KV parity holds; in
        # chat mode this is a self-contained off-turn lane.
        offer_tools=_OFFER_TOOLS,
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    logger.info("[image_gen] %s returned: %s", tool_name, args)
    return args


_COUNT_TOKEN = r"(?:\d+\+?\s*(?:girls?|boys?|others?)|multiple\s+(?:girls|boys|others)|solo|pov)"
_COUNT_CHUNK_RE = re.compile(rf"{_COUNT_TOKEN}(?:\s+{_COUNT_TOKEN})*", re.IGNORECASE)
# CLIP has no negation: a "no longer wearing X" chunk copied through to the
# image prompt draws X. Drop any chunk that negates, in every mode -- the phrase
# can sit anywhere in the chunk, so this matches by search, not just at the
# start. In analysis mode the removed articles are re-enforced via the negative;
# single-call has no delta to route, so the removal is simply dropped, which
# still beats drawing the item.
_NEGATION_CHUNK_RE = re.compile(r"(?:no longer wearing|not wearing|without)\b", re.IGNORECASE)


def _strip_chunks(text: str, pattern: re.Pattern, *, whole: bool = True) -> str:
    """Drop comma chunks the pattern hits. `whole` matches a chunk that IS the
    pattern (count blocks); otherwise the pattern need only appear inside it."""
    hit = pattern.fullmatch if whole else pattern.search
    return ", ".join(c for c in (c.strip() for c in text.split(",")) if c and not hit(c))


def _count_anchor(characters: Any) -> str | None:
    """Booru count tags from the analyzed cast, e.g. '1boy, 1girl' or '1girl, solo'.

    The analyze schema already excludes the viewer character in first_person, so
    counting this list is what guarantees POV scenes never leak the extra '1boy'.
    Returns None when any entry is malformed or missing a sex -- caller skips
    pinning rather than guess.
    """
    counts = dict.fromkeys(("girl", "boy", "other"), 0)
    for ch in characters if isinstance(characters, list) else [None]:
        sex = _bounded(ch.get("sex")).lower() if isinstance(ch, Mapping) else ""
        if sex not in counts:
            return None
        counts[sex] += 1
    parts = [f"{n}{sex}" + ("s" if n > 1 else "") for sex, n in counts.items() if n]
    if sum(counts.values()) == 1:
        parts.append("solo")
    return ", ".join(parts)


def _pin_anchor(scene: str, anchor: str, pov: bool) -> str:
    """Deterministically own the count block: drop whatever counts the composer wrote."""
    lead = ([anchor] if anchor else []) + (["pov"] if pov else [])
    kept = _strip_chunks(scene, _COUNT_CHUNK_RE)
    return ", ".join(lead + [kept] if kept else lead) or scene


def _split_lead_count(scene: str) -> tuple[str, str]:
    """Peel the leading run of count/pov chunks off the scene, so the caller can
    seat the count anchor at the very head of the final prompt (booru training
    puts counts first; a long appearance in front pushes them out of CLIP's first
    77-token window). Returns (count_lead, remainder)."""
    parts = [c.strip() for c in scene.split(",") if c.strip()]
    lead = 0
    while lead < len(parts) and _COUNT_CHUNK_RE.fullmatch(parts[lead]):
        lead += 1
    return ", ".join(parts[:lead]), ", ".join(parts[lead:])


def _render_scene(scene: Any) -> str:
    """Structured analyze_scene args -> compact text for the composition call.

    Tolerant of missing/malformed fields: any absent character or section is
    dropped, so a partial scene from the model still yields usable text.
    """
    if not isinstance(scene, Mapping):
        return ""
    lines: list[str] = []
    if _bounded(scene.get("viewpoint")) == "first_person":
        lines.append("viewpoint: first-person POV (pov) -- the viewer character is not drawn, hands at most")
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
    scene_analysis: bool = False,
    appearance: str = "",
) -> tuple[str, str, str]:
    """Compose the scene text for one message.

    Returns ``(scene, avoid, mode)``. The profile appearance is always
    prepended by the caller: the target character (the profile owner -- the
    "main character" the analyze contract names, NOT the user, who is the
    excluded viewer in first-person POV) has a fixed look (e.g. a character
    tag) that must show up whether or not the analyzer thinks they are
    on-frame -- the empty-appearance "off-frame" heuristic over-fired
    whenever the model filled appearance for every character.

    Raises ``ValueError`` when the forced compose call yields no scene: the
    generation stops rather than falling back to the raw reply text. There is
    no excerpt fallback -- see the scene-guard note below.

    Both LLM calls ride *prefix* unchanged -- the same byte-identical
    conversation prefix the chat turns send -- so the server's cached KV is
    reused, not evicted, across analyze -> compose -> the next chat turn.
    Everything per-call rides the tail messages after it.
    """
    # One reasoning mode for both forced calls, so analyze and compose stay
    # mode-identical and reuse each other's cached prefix on the same lane.
    reasoning_on = _reasoning_on(settings)

    analysis: dict = {}
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
            reasoning_on=reasoning_on,
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
        reasoning_on=reasoning_on,
    )

    # Strip negations in every mode: CLIP draws "no longer wearing X" as X, so no
    # composed prompt may carry one, analysis path or not.
    scene = _strip_chunks(_bounded(args.get("scene")), _NEGATION_CHUNK_RE, whole=False)
    if not scene:
        # No excerpt fallback. When the forced call produces no scene, stop --
        # do not ship the raw reply text to the diffusion model as the image
        # prompt. Prose is exactly what the tag-trained checkpoints render as
        # washed-out mush (see the plan's "composer's output format is a real
        # design risk"), so an excerpt fallback trades a clean failure for a
        # bad image. Callers already degrade on this: on-demand surfaces the
        # error, regenerate/reroll drop the attachment.
        raise ValueError("couldn't compose an image prompt for this message")
    avoid = _bounded(args.get("avoid"))
    if analysis_block:
        characters = [ch for ch in analysis.get("characters") or [] if isinstance(ch, Mapping)]
        anchor = _count_anchor(analysis.get("characters"))
        if anchor is not None:
            scene = _pin_anchor(scene, anchor, _bounded(analysis.get("viewpoint")) == "first_person")
        # `hidden` (turned-away/occluded/cropped) and removed outfit articles are
        # enforced from the negative side; the positive prompt can't say "not X"
        # in a way CLIP respects.
        avoid = _join([args.get("avoid"), analysis.get("hidden"), *(ch.get("outfit_removed") for ch in characters)])
    if analysis_block:
        mode = "scene_analysis"
    elif scene_analysis:
        mode = "analysis_failed"
    else:
        mode = "single_call"
    return scene, avoid, mode


def assemble_prompts(
    config: Mapping[str, Any],
    style_id: str,
    profile: Mapping[str, Any],
    scene: str,
    avoid: str,
) -> tuple[str, str, dict]:
    style = resolve_style(config, style_id)
    # Counts and pov belong to the scene's anchor; a profile that opens with
    # "1girl," would duplicate or fight it from in front of the anchor.
    # ponytail: always prepended. The one shot this over-includes is a POV
    # *through the target character's own eyes* (they're the viewer, shouldn't
    # be drawn) -- rare, and the analyzer can't tell it apart from the common
    # POV (user's eyes on the target) anyway, which is why the old auto-drop
    # over-fired. Gate on an explicit "target is the viewer" signal if it bites.
    appearance = _strip_chunks(_bounded(profile.get("appearance_prompt")), _COUNT_CHUNK_RE)
    # Count anchor leads the whole prompt, appearance right behind it: booru
    # training weights the first tags heaviest, and the prepended appearance was
    # pushing the anchor back out of CLIP's first window.
    count_lead, scene_body = _split_lead_count(scene)
    positive = _join((count_lead, appearance, scene_body, style.get("prompt")))
    negative = _join((profile.get("negative_prompt"), avoid, style.get("negative_prompt")))
    return positive, negative, style
