"""The scene composition flow: what to ask the model, in what order, and what to
do with the answer.

The two halves it coordinates live next door -- `prompts.py` owns every instruction
string and schema, `scrub.py` owns the deterministic text surgery applied to the
result. What is left here is the sequencing: analyze (optionally), compose, pin the
count anchor, inject the fixed appearance, assemble the final prompt pair.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import forced_tool_call
from .config import DEFAULT_PROMPT_FORMAT, resolve_style
from .pov import FIRST, THIRD
from .prompts import OFFER_TOOLS, analyze_ooc, compose_ooc
from .scrub import (
    SubjectAppearance,
    bounded,
    clean_scene,
    count_anchor,
    inject_profile_appearance,
    join,
    normalize_prompt_format,
    pin_anchor,
    split_lead_count,
    strip_count_tags,
    strip_prose_count_prefix,
)
from .subjects import Subject

logger = logging.getLogger(__name__)


async def _forced_args(*, client, model_name, prefix, tail, tool_name, settings, max_tokens, reasoning_on) -> dict:
    # Debug: the per-call instruction actually sent. Set this logger to WARNING to silence.
    logger.info("[image_gen] %s tail:\n%s", tool_name, "\n--\n".join(m["content"] for m in tail))
    args: dict = {}
    async for event in forced_tool_call(
        client=client,
        prefix=prefix,
        tail_messages=tail,
        tool_name=tool_name,
        settings=settings,
        model_name=model_name,
        # One workflow-owned mode for both calls, so they share a reasoning-forked lane.
        reasoning_on=reasoning_on,
        temperature=0.2,
        max_tokens=max_tokens,
        offer_tools=OFFER_TOOLS,
    ):
        if event.get("type") == "result" and isinstance(event.get("args"), dict):
            args = event["args"]
    logger.info("[image_gen] %s returned: %s", tool_name, args)
    return args


# One analyzed character, rendered in reading order, with the label each field
# carries into the block.
_CHARACTER_FIELDS = (
    ("face_view", ""),
    ("position", ""),
    ("pose", ""),
    ("action", ""),
    ("appearance", ""),
    ("outfit", "wearing: "),
    ("expression", "expression: "),
    ("gaze", "gaze: "),
)


def _render_scene(scene: Any, pov: str) -> str:
    """Structured analyze_scene args -> compact text for the composition call.

    States no viewpoint: the compose OOC renders this block *exactly*, so every
    word here may reach the image model, and "camera" draws a literal camera. Shot
    rules live in the OOC head (`_format_guide`). *pov* only selects whether the
    viewer-contact line is read, so an analyzer that filled it in third-person is
    corrected here.

    Tolerant of missing/malformed fields; an analysis with no content at all
    renders empty, which is what tells `compose_scene` the analyze call failed.
    """
    if not isinstance(scene, Mapping):
        return ""
    header: list[str] = []
    lines: list[str] = []
    if pov == FIRST:
        contact = bounded(scene.get("viewer_contact"))
        if contact:
            header.append(f"the user's hand or arm in frame: {contact}")
    for ch in scene.get("characters") or []:
        if not isinstance(ch, Mapping):
            continue
        name = bounded(ch.get("name")) or "character"
        labels: list[str] = []
        sex = bounded(ch.get("sex")).lower()
        if sex in ("girl", "boy", "other"):
            labels.append(sex)
        if ch.get("is_listed_subject") is True:
            labels.append("subject")
        if labels:
            name += " [" + "; ".join(labels) + "]"
        # View and pose first, so the composer commits to the shot before listing
        # attributes. `expression` is dropped when the analyzer flagged the face
        # hidden; `face_view` is the analyzer's own words, so an absent one is left
        # unstated rather than guessed at as a back view.
        face_visible = ch.get("face_visible") is not False
        bits = [
            f"{prefix}{value}"
            for key, prefix in _CHARACTER_FIELDS
            if (value := bounded(ch.get(key))) and (face_visible or key != "expression")
        ]
        if bits:
            lines.append(f"{name}: " + ", ".join(bits))
    interaction = bounded(scene.get("interaction"))
    if interaction:
        lines.append(f"interaction: {interaction}")
    tail = join((scene.get("setting"), scene.get("anchors"), scene.get("framing")))
    if tail:
        lines.append(f"setting and framing: {tail}")
    return "\n".join(header + lines) if lines else ""


def _cast_entries(analysis: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [ch for ch in analysis.get("characters") or [] if isinstance(ch, Mapping)]


def _matched(subjects: Sequence[SubjectAppearance], analysis: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    """Which analyzed cast entry is which subject, by subject index.

    Matched on the name, which is why both prompts quote the roster back and tell the
    model to copy it exactly: with several subjects, a name is the only thing that can
    say *which* of them an entry is. Each entry is claimed at most once, so two members
    the model gave one name do not both collapse onto the first subject.

    `is_listed_subject` is honoured as a fallback for a lone subject only. It carries no
    identity past that -- flagging two entries says they are both subjects, not which is
    which -- but for one subject it preserves the behaviour the singular flag had: a
    model that wrote "the woman" instead of "Mara" still keeps her saved appearance.
    """
    entries = _cast_entries(analysis)
    matched: dict[int, Mapping[str, Any]] = {}
    claimed: set[int] = set()
    for index, subject in enumerate(subjects):
        name = bounded(subject.name, 200).casefold()
        if not name:
            continue
        for position, entry in enumerate(entries):
            if position in claimed or bounded(entry.get("name"), 200).casefold() != name:
                continue
            matched[index] = entry
            claimed.add(position)
            break
    if len(subjects) == 1 and 0 not in matched:
        flagged = next((entry for entry in entries if entry.get("is_listed_subject") is True), None)
        if flagged is not None:
            matched[0] = flagged
    return matched


def _keep_subjects(analysis: dict, subjects: Sequence[SubjectAppearance]) -> None:
    """First-person looks through the user's eyes at the subject, so drop every other
    visible character. No-op when no subject is in the analyzed cast, so a first-person
    view of someone else keeps its cast rather than emptying it.

    `subjects` is already truncated to one under first-person (`subjects.resolve`), so
    this is that same rule applied to the analyzer's answer rather than a second one.
    """
    if not isinstance(analysis.get("characters"), list):
        return
    kept = list(_matched(subjects, analysis).values())
    if kept:
        analysis["characters"] = kept


def _visible(subjects: Sequence[SubjectAppearance], analysis: Mapping[str, Any]) -> list[SubjectAppearance]:
    """The subjects the analyzer put in frame, in subject order, with their face state.

    A subject the analyzer never listed is not in the picture and contributes nothing:
    injecting a fixed appearance for someone who left the room is how a saved sheet
    draws a second person into the shot.
    """
    matched = _matched(subjects, analysis)
    return [
        subject._replace(face_visible=matched[index].get("face_visible") is not False)
        for index, subject in enumerate(subjects)
        if index in matched
    ]


def _named_visible(subjects: Sequence[SubjectAppearance], names: Any) -> list[SubjectAppearance]:
    """The same answer off the single-call path, where the composer names them itself.

    Faces are unknown without an analysis, so every match keeps its whole sheet --
    which is what the singular `profile_owner_visible` path already did.
    """
    listed = {bounded(name, 200).casefold() for name in names if isinstance(name, str)} if isinstance(names, list) else set()
    return [subject for subject in subjects if bounded(subject.name, 200).casefold() in listed]


async def compose_scene(
    *,
    client: Any,
    model_name: str,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    prompt_format: str = DEFAULT_PROMPT_FORMAT,
    pov: str = THIRD,
    reasoning_on: bool = False,
    scene_analysis: bool = False,
    subjects: Sequence[Subject] = (),
    extra_instructions: str = "",
    supports_negative: bool = True,
    has_references: bool = False,
    referenced_subjects: Sequence[str] = (),
    style_prompt: str = "",
    style_negative_prompt: str = "",
    profile_negative_prompt: str = "",
) -> tuple[str, str, str]:
    """Compose the scene text for one message, as ``(scene, avoid, mode)``.

    *subjects* is ``subjects.resolve``'s ordered answer -- who this render is *of* --
    and is the same list the reference slots were filled from. *referenced_subjects*
    names the ones whose likeness actually went with the request, in slot order, which
    is what stops the reference instruction from suppressing identity traits for
    everybody else in the frame.

    *pov* is already resolved (``pov.resolve``) and selects which mode's
    instructions both calls carry. It never reaches the tool schemas: those ship as
    one byte-stable blob so a camera switch costs no cached prefix. Both calls ride
    *prefix* unchanged -- the byte-identical conversation prefix the chat turns
    send -- so the server's cached KV survives analyze -> compose -> the next turn.

    Raises ``ValueError`` when the forced compose call yields no scene, rather than
    falling back to the raw reply text.
    """
    # One projection of the subject list, shared by both calls and by the injector, so
    # the roster the model is shown is the roster the fixed tags are drawn from.
    sheets = [
        SubjectAppearance(name=subject.name, appearance=str(subject.profile.get("appearance_prompt") or ""))
        for subject in subjects
    ]
    analysis: dict = {}
    analysis_block = ""
    if scene_analysis:
        analysis = await _forced_args(
            client=client,
            model_name=model_name,
            prefix=prefix,
            tail=[{"role": "user", "content": analyze_ooc(pov, supports_negative, sheets)}],
            tool_name="analyze_scene",
            settings=settings,
            max_tokens=2_048,
            reasoning_on=reasoning_on,
        )
        # First-person view is the user looking at the subject: keep only the subject
        # so a stray background character does not get drawn into the shot.
        if pov == FIRST:
            _keep_subjects(analysis, sheets)
        analysis_block = _render_scene(analysis, pov)

    tail = [
        {
            "role": "user",
            "content": compose_ooc(
                prompt_format,
                pov,
                structured=bool(analysis_block),
                subjects=sheets,
                extra_instructions=extra_instructions,
                supports_negative=supports_negative,
                has_references=has_references,
                referenced_subjects=referenced_subjects,
                style_prompt=style_prompt,
                style_negative_prompt=style_negative_prompt,
                profile_negative_prompt=profile_negative_prompt,
            ),
        }
    ]
    if analysis_block:
        # The scene rides last, where attention is strongest: the composer renders
        # exactly this block instead of re-deriving it.
        tail.append({"role": "user", "content": "Structured scene extracted from the conversation:\n\n" + analysis_block})
    args = await _forced_args(
        client=client,
        model_name=model_name,
        prefix=prefix,
        tail=tail,
        tool_name="compose_image_prompt",
        settings=settings,
        max_tokens=4_096,
        reasoning_on=reasoning_on,
    )

    normalized_format = normalize_prompt_format(prompt_format)
    scene = clean_scene(bounded(args.get("scene")), prompt_format=prompt_format, pov=pov)
    if not scene:
        # No excerpt fallback: the raw reply is narration and dialogue, so shipping
        # it would trade a clean failure for a bad image. Callers already degrade --
        # on-demand surfaces the error, regenerate/reroll drop the attachment.
        raise ValueError("couldn't compose an image prompt for this message")
    avoid = bounded(args.get("avoid"))
    if analysis_block:
        anchor = count_anchor(analysis.get("characters"))
        if anchor is not None and normalized_format != "prose":
            scene = pin_anchor(scene, anchor)
        avoid = join([args.get("avoid"), analysis.get("avoid")])
        visible = _visible(sheets, analysis)
    else:
        visible = _named_visible(sheets, args.get("visible_subjects"))
    # One call, in subject order: injecting per subject would stack them in reverse.
    scene = inject_profile_appearance(scene, visible, prompt_format)
    mode = "scene_analysis" if analysis_block else ("analysis_failed" if scene_analysis else "single_call")
    return scene, avoid, mode


def assemble_prompts(
    config: Mapping[str, Any],
    style_id: str,
    profile: Mapping[str, Any],
    scene: str,
    avoid: str,
) -> tuple[str, str, dict]:
    style = resolve_style(config, style_id)
    # Count tags are valid only for tags/hybrid, enforced again here so a stale
    # scene or a saved style block cannot bypass the composer-side prose guard.
    prompt_format = normalize_prompt_format(str(style.get("prompt_format") or ""))
    if prompt_format == "prose":
        scene_body = strip_prose_count_prefix(scene)
        style_prompt = strip_count_tags(bounded(style.get("prompt")))
        positive = strip_count_tags(join((style_prompt, scene_body)))
    else:
        # Keep the count anchor at the head, then apply the style before the
        # subject and setting details it governs.
        count_lead, scene_body = split_lead_count(scene)
        positive = join((count_lead, style.get("prompt"), scene_body))
    negative = join((profile.get("negative_prompt"), avoid, style.get("negative_prompt")))
    return positive, negative, style
