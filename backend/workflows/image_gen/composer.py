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


def _matched(names: Sequence[str], analysis: Mapping[str, Any]) -> dict[int, Mapping[str, Any]]:
    """Which analyzed cast entry is which subject, by subject index.

    Matched on the name, which is why both prompts quote the roster back and tell the
    model to copy it exactly: with several subjects, a name is the only thing that can
    say *which* of them an entry is. Each entry is claimed at most once, so two members
    the model gave one name do not both collapse onto the first subject -- and
    `subjects.resolve` has already made the roster's own names distinct, so the model
    is never asked to tell two "Guard"s apart in the first place.

    Takes bare names because that is all it reads: the same answer serves the appearance
    injector, the first-person trim, and the reference slots, which hold three different
    subject shapes between them.

    `is_listed_subject` is honoured as a fallback for a lone subject only. It carries no
    identity past that -- flagging two entries says they are both subjects, not which is
    which -- but for one subject it preserves the behaviour the singular flag had: a
    model that wrote "the woman" instead of "Mara" still keeps her saved appearance.
    """
    entries = _cast_entries(analysis)
    matched: dict[int, Mapping[str, Any]] = {}
    claimed: set[int] = set()
    for index, subject_name in enumerate(names):
        name = bounded(subject_name, 200).casefold()
        if not name:
            continue
        for position, entry in enumerate(entries):
            if position in claimed or bounded(entry.get("name"), 200).casefold() != name:
                continue
            matched[index] = entry
            claimed.add(position)
            break
    if len(names) == 1 and 0 not in matched:
        flagged = next((entry for entry in entries if entry.get("is_listed_subject") is True), None)
        if flagged is not None:
            matched[0] = flagged
    return matched


def _report_binding(where: str, names: Sequence[str], matched: Mapping[int, Any]) -> None:
    """Say when a subject failed to bind, because nothing else will.

    A subject the model renamed drops out of the match and loses its saved appearance
    sheet -- and the render still succeeds, still looks plausible, and is quietly a
    picture of someone slightly else. That is the one failure in this workflow with no
    user-visible symptom, so it gets a log line rather than silence.
    """
    missing = [name for index, name in enumerate(names) if index not in matched and bounded(name, 200)]
    if missing:
        logger.info(
            "[image_gen] %s: %d/%d subjects bound; the model did not name %s",
            where,
            len(matched),
            len(names),
            ", ".join(repr(name) for name in missing),
        )


def _keep_subjects(analysis: dict, subjects: Sequence[SubjectAppearance]) -> None:
    """First-person looks through the user's eyes at the subject, so drop every other
    visible character. No-op when no subject is in the analyzed cast, so a first-person
    view of someone else keeps its cast rather than emptying it.

    `subjects` is already truncated to one under first-person (`subjects.resolve`), so
    this is that same rule applied to the analyzer's answer rather than a second one.
    """
    if not isinstance(analysis.get("characters"), list):
        return
    kept = list(_matched([subject.name for subject in subjects], analysis).values())
    if kept:
        analysis["characters"] = kept


def _visible(subjects: Sequence[SubjectAppearance], analysis: Mapping[str, Any]) -> list[SubjectAppearance]:
    """The subjects the analyzer put in frame, in subject order, with their face state.

    A subject the analyzer never listed is not in the picture and contributes nothing:
    injecting a fixed appearance for someone who left the room is how a saved sheet
    draws a second person into the shot.
    """
    names = [subject.name for subject in subjects]
    matched = _matched(names, analysis)
    _report_binding("analysis", names, matched)
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
    matched = {index: subject for index, subject in enumerate(subjects) if bounded(subject.name, 200).casefold() in listed}
    _report_binding("single call", [subject.name for subject in subjects], matched)
    return list(matched.values())


def _sheets(subjects: Sequence[Subject]) -> list[SubjectAppearance]:
    """One projection of the subject list, shared by both calls and by the injector, so
    the roster the model is shown is the roster the fixed tags are drawn from."""
    return [
        SubjectAppearance(name=subject.name, appearance=str(subject.profile.get("appearance_prompt") or ""))
        for subject in subjects
    ]


async def analyze_scene(
    *,
    client: Any,
    model_name: str,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    pov: str = THIRD,
    reasoning_on: bool = False,
    subjects: Sequence[Subject] = (),
    supports_negative: bool = True,
) -> dict:
    """The analyzer pass on its own: who is in frame, and how they look right now.

    **Separate from the compose call on purpose.** Its answer decides who is in the
    picture, and that has to be known *before* the reference slots are filled -- a
    likeness uploaded for a member the analyzer left out of frame is an edit model's
    invitation to draw them back in, against a prompt that never mentions them. So the
    caller runs this, fills slots from `addressable_subjects`, and only then composes.

    Rides *prefix* unchanged, exactly as the compose call does, so splitting the two
    costs no cached KV: the tails differ, the prefix does not.

    Answers `{}` when the forced call yields nothing, which the composer reports as
    `analysis_failed` and which `addressable_subjects` treats as "no answer" rather
    than as "nobody is visible".
    """
    analysis = await _forced_args(
        client=client,
        model_name=model_name,
        prefix=prefix,
        tail=[{"role": "user", "content": analyze_ooc(pov, supports_negative, _sheets(subjects))}],
        tool_name="analyze_scene",
        settings=settings,
        max_tokens=2_048,
        reasoning_on=reasoning_on,
    )
    # First-person view is the user looking at the subject: keep only the subject
    # so a stray background character does not get drawn into the shot.
    if pov == FIRST:
        _keep_subjects(analysis, _sheets(subjects))
    return analysis


def addressable_subjects(subjects: Sequence[Subject], analysis: Mapping[str, Any] | None) -> tuple[Subject, ...]:
    """Whose likeness a reference slot may actually draw, in `cast` ordinal order.

    **Subject 0 is unconditional.** `character` means the render's primary, and a solo
    chat's one slot has to resolve whether or not the analyzer bothered to list anyone;
    filtering it would turn a landscape shot of an established character into a hard
    failure on every ComfyUI graph built around a `LoadImage`.

    **The tail is filtered**, because that is the part a group added and the part that
    can go wrong. A member who spoke in the beat but whom the analyzer left out of frame
    contributes nothing to the prompt -- `_visible` already drops their sheet -- so
    uploading their face anyway sends the image model a person the words never mention.

    Compacted, not sparse: dropping subject 1 means the first `cast` slot draws subject
    2, which is what "the next cast member" means to someone reading the picker. The
    slots address positions in *this* list.

    No analysis -- the single-call path, or a forced call that came back empty -- is not
    an answer of "nobody", so nothing is filtered.
    """
    if not analysis or len(subjects) < 2:
        return tuple(subjects)
    matched = _matched([subject.name for subject in subjects], analysis)
    return (subjects[0], *(subject for index, subject in enumerate(subjects) if index and index in matched))


async def compose_scene(
    *,
    client: Any,
    model_name: str,
    prefix: Sequence[dict],
    settings: Mapping[str, Any],
    prompt_format: str = DEFAULT_PROMPT_FORMAT,
    pov: str = THIRD,
    reasoning_on: bool = False,
    analysis: Mapping[str, Any] | None = None,
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

    *subjects* is ``subjects.resolve``'s ordered answer -- who this render is *of*.
    *referenced_subjects* names the ones whose likeness actually went with the request,
    in slot order, which is what stops the reference instruction from suppressing
    identity traits for everybody else in the frame.

    *analysis* is ``analyze_scene``'s answer, or ``None`` when the user has scene
    analysis off. It arrives already made rather than being taken here, because the
    reference slots the caller filled in between depend on it -- see
    ``addressable_subjects``. ``{}`` is a *failed* analysis and reports as
    ``analysis_failed``; ``None`` is a render that never asked for one.

    *pov* is already resolved (``pov.resolve``) and selects which mode's
    instructions both calls carry. It never reaches the tool schemas: those ship as
    one byte-stable blob so a camera switch costs no cached prefix. Both calls ride
    *prefix* unchanged -- the byte-identical conversation prefix the chat turns
    send -- so the server's cached KV survives analyze -> compose -> the next turn.

    Raises ``ValueError`` when the forced compose call yields no scene, rather than
    falling back to the raw reply text.
    """
    sheets = _sheets(subjects)
    analysis_block = _render_scene(analysis, pov) if analysis else ""

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
    if analysis_block and analysis is not None:
        anchor = count_anchor(analysis.get("characters"))
        if anchor is not None and normalized_format != "prose":
            scene = pin_anchor(scene, anchor)
        avoid = join([args.get("avoid"), analysis.get("avoid")])
        visible = _visible(sheets, analysis)
    else:
        visible = _named_visible(sheets, args.get("visible_subjects"))
    # One call, in subject order: injecting per subject would stack them in reverse.
    scene = inject_profile_appearance(scene, visible, prompt_format)
    # `None` never asked for an analysis; `{}` asked and the forced call came back empty.
    mode = "scene_analysis" if analysis_block else ("single_call" if analysis is None else "analysis_failed")
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
