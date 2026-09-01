"""Clean and normalize composed image prompts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from .config import DEFAULT_PROMPT_FORMAT, PROMPT_FORMATS
from .pov import FIRST


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


_COUNT_TOKEN = r"(?:\d+\+?\s*(?:girls?|boys?|others?)|multiple\s+(?:girls|boys|others)|solo|pov)"


_COUNT_CHUNK_RE = re.compile(rf"{_COUNT_TOKEN}(?:\s+{_COUNT_TOKEN})*", re.IGNORECASE)


_PROSE_COUNT_PREFIX_RE = re.compile(rf"^(?:(?:{_COUNT_TOKEN})\b\s*[,.;:]?\s*)+", re.IGNORECASE)


# CLIP has no negation: a "no longer wearing X" chunk drawn through to the image
# prompt draws X. The phrase can sit anywhere in the chunk, so this searches.
_NEGATION_CHUNK_RE = re.compile(r"(?:no longer wearing|not wearing|without)\b", re.IGNORECASE)


_POV_CHUNK_RE = re.compile(r"pov", re.IGNORECASE)


# A text encoder has no idea "camera" is meta: it draws one, in frame, in someone's
# hands. The word is unavoidable in the instructions, so drop the whole chunk that
# echoes it back. Word-bounded search so "camerawork" goes too while the real booru
# tag "looking at viewer" survives.
_CAMERA_CHUNK_RE = re.compile(r"\bcamera\w*", re.IGNORECASE)


# The composer writes the literal contact ("arm gripping viewer's shirt collar")
# because that is what happened. The encoder has never seen it -- booru tagged the
# reach *toward* the lens, never the viewer's body -- so the literal phrase draws
# an improvised limb. The model states the fact; this table owns the vocabulary.
_VIEWER_RE = re.compile(r"\b(?:viewer|user|your)'?s?\b", re.IGNORECASE)


# Mentioning the viewer is not touching them: giving "standing close to the viewer"
# the reach tags invents an arm nobody wrote, which is worse than losing the chunk.
# "pin" and "cup" are real words on their own, unlike the other stems here, so their
# non-contact tails are excluded rather than the stems dropped.
_VIEWER_CONTACT_VERB_RE = re.compile(
    r"\b(?:grab|grip|grasp|clutch|clasp|pull|tug|yank|hold|shov|push|press|pin(?!-?up)|touch|caress|"
    r"cup(?!cake|board)|stroke|squeez|reach|kiss|hug|embrac|bit|lick|slap|punch|strik|hit|chok|strangl|throttl)",
    re.IGNORECASE,
)


# Gaze is the other thing said about the viewer, and booru has one tag for it.
_VIEWER_GAZE_RE = re.compile(r"\b(?:look|gaz|star|glanc|watch|eyes?)\w*\b[^,]*\b(?:viewer|user|your)", re.IGNORECASE)


_LOOKING_AT_VIEWER = "looking at viewer"


# Only the noun after the possessive says which side of the contact the viewer is
# on. A viewer's *limb* is the user acting (the shot rules ask for it by name);
# anything else the possessive owns -- throat, collar, chest -- is the viewer being
# acted upon, and their body must not be drawn. Keep and retag the first, collapse
# the second.
_VIEWER_LIMB_RE = re.compile(
    r"\b(?:viewer|user|your)'?s?\s+(?:hands?|arms?|fingers?|palms?|fists?|wrists?|thumbs?)\b",
    re.IGNORECASE,
)


_VIEWER_POSSESSIVE_RE = re.compile(r"\b(?:the\s+)?(?:viewer|user|your)'?s?\s+", re.IGNORECASE)


# What booru calls the user's own hand entering frame. Survives the bare-"pov"
# strip above, which fullmatches the chunk rather than searching it.
_POV_HANDS = "pov hands"


_VIEWER_CONTACT_TAGS = (
    (re.compile(r"\bkiss", re.IGNORECASE), "incoming kiss, close-up, foreshortening"),
    (re.compile(r"\b(?:hug|embrac)", re.IGNORECASE), "incoming hug, outstretched arms, foreshortening"),
    # Approximate by design: a gentle hand on the neck lands here too, and the
    # arm-toward-lens shape is close enough to be right.
    (
        re.compile(r"\b(?:strangl|chok|throttl|throat|neck)", re.IGNORECASE),
        "strangling, reaching towards viewer, foreshortening",
    ),
    (re.compile(r"\b(?:punch|slap|strik|attack|swing)", re.IGNORECASE), "incoming attack, outstretched arm, foreshortening"),
    # Contact without a reach: the fallback's outstretched arm would be flatly wrong.
    (re.compile(r"\b(?:straddl|lap|sitting on|riding|on top of)", re.IGNORECASE), "on top, straddling, foreshortening"),
    # A levelled weapon touches nothing, so the contact gate below would drop it.
    (re.compile(r"\b(?:aim|point|knife|gun|blade|sword|weapon|barrel)", re.IGNORECASE), "aiming at viewer, foreshortening"),
    # Body against body: fills the frame rather than extending toward it. Last row,
    # so a lean that is really a kiss or a straddle matches those first.
    (re.compile(r"\b(?:lean|nuzzl|snuggl|nestl|against|resting on)", re.IGNORECASE), "close-up, foreshortening"),
)


# Everything else that touches the viewer collapses to the one composition booru
# has in volume: arm out, hand toward the lens, the rest cropped past the wrist.
_VIEWER_FALLBACK = "reaching beyond edge of screen, foreshortening"


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


def rewrite_viewer_contact(text: str) -> str:
    """Swap chunks that name contact with the viewer for tags the encoder knows.

    Contact collapses to a tag, the viewer's own limb keeps its action, gaze
    normalizes, and a chunk that merely mentions the viewer is dropped. Nothing
    naming the viewer survives verbatim: kept, it draws the viewer's own body into
    frame, the one thing a first-person shot must not contain.
    """
    out: list[str] = []
    for chunk in (c.strip() for c in text.split(",")):
        if not chunk:
            continue
        if not _VIEWER_RE.search(chunk):
            out.append(chunk)
            continue
        if _VIEWER_LIMB_RE.search(chunk):
            # The viewer's own limb, acting: keep what it does (the subject of the
            # shot), drop only the possessive the encoder cannot place.
            if _POV_HANDS not in out:
                out.append(_POV_HANDS)
            out.append(_VIEWER_POSSESSIVE_RE.sub("", chunk).strip())
            continue
        tag = next((t for pat, t in _VIEWER_CONTACT_TAGS if pat.search(chunk)), None)
        if tag is None and _VIEWER_CONTACT_VERB_RE.search(chunk):
            tag = _VIEWER_FALLBACK
        if tag is None:
            # No contact. Gaze is the only other thing worth keeping; naming the
            # viewer any other way draws them.
            tag = _LOOKING_AT_VIEWER if _VIEWER_GAZE_RE.search(chunk) else ""
        # Two chunks describing one grab land on the same tag; emit it once.
        if tag and tag not in out:
            out.append(tag)
    return ", ".join(out)


def count_anchor(characters: Any) -> str | None:
    """Booru count tags from the analyzed cast, e.g. '1boy, 1girl' or '1girl, solo'.

    The analyze schema excludes the viewer in first_person, so counting this list
    is what keeps POV scenes from leaking an extra '1boy'. None when an entry is
    malformed or missing a sex -- the caller skips pinning rather than guess.
    """
    counts = dict.fromkeys(("girl", "boy", "other"), 0)
    for ch in characters if isinstance(characters, list) else [None]:
        sex = bounded(ch.get("sex")).lower() if isinstance(ch, Mapping) else ""
        if sex not in counts:
            return None
        counts[sex] += 1
    parts = [f"{n}{sex}" + ("s" if n > 1 else "") for sex, n in counts.items() if n]
    if sum(counts.values()) == 1:
        parts.append("solo")
    return ", ".join(parts)


def pin_anchor(scene: str, anchor: str) -> str:
    """Deterministically own the count block: drop whatever counts the composer wrote."""
    lead = [anchor] if anchor else []
    kept = strip_chunks(scene, _COUNT_CHUNK_RE)
    return ", ".join(lead + [kept] if kept else lead) or scene


def split_lead_count(scene: str) -> tuple[str, str]:
    """Peel the leading count/pov chunks off, as ``(count_lead, remainder)``.

    Booru training puts counts first, and a long appearance in front of them pushes
    them out of CLIP's first 77-token window.
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

    The pure projection of a `subjects.Subject`: a name, the fixed tags, and whether
    the analyzer could see this person's face. Declared here rather than imported
    because this module reads no config and calls no model, and the composer is what
    turns a resolved subject and an analysis into one of these.
    """

    name: str
    appearance: str
    face_visible: bool = True


def inject_profile_appearance(scene: str, subjects: Sequence[SubjectAppearance], prompt_format: str) -> str:
    """Insert fixed appearance traits for visible subjects."""
    normalized_format = normalize_prompt_format(prompt_format)
    blocks: list[tuple[str, str]] = []
    for subject in subjects:
        fixed = strip_chunks(bounded(subject.appearance), _COUNT_CHUNK_RE)
        fixed = strip_chunks(fixed, _NEGATION_CHUNK_RE, whole=False)
        if not subject.face_visible:
            fixed = strip_chunks(fixed, _FACE_CHUNK_RE, whole=False)
        if fixed:
            blocks.append((bounded(subject.name, 200), fixed))
    if not blocks:
        return scene

    def render(name: str, fixed: str) -> str:
        if not name or (normalized_format == "tags" and len(blocks) == 1):
            return fixed
        return f"{name} has these traits: {fixed}." if normalized_format == "prose" else f"{name}: {fixed}"

    rendered = [render(name, fixed) for name, fixed in blocks]
    # Seated right after the count anchor, so identity stays near the high-attention
    # head rather than landing after the setting.
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


def clean_scene(scene: str, *, prompt_format: str, pov: str) -> str:
    """Every scrub the raw composer output goes through, in one pass.

    Gathered here rather than spelled out at the call site so `compose_scene` states
    *that* the scene is cleaned without owning a list of regexes -- and so the whole
    pass can be asserted against a literal string with no model in the loop.
    """
    if normalize_prompt_format(prompt_format) == "prose":
        # Prose goes to a natural-language encoder, and every rewrite below answers a
        # booru/CLIP failure that encoder does not have: it reads negation, it takes
        # "camera" as the framing word every photo caption uses, and it parses
        # grammar. Scrubbing it there only costs the model its wording -- and because
        # a comma bounds nothing in prose, the comma-chunk cut took whole sentences
        # back to the previous comma, or the entire scene when it had no commas.
        # So prose keeps what the composer wrote, the call `rewrite_viewer_contact`
        # already makes. Leaked booru count tags still go: those are the tail's own
        # format rule broken, not a choice of words.
        return strip_prose_count_prefix(scene)
    # A count block ended with a period ("1boy, 1girl. Gon eats...") would hide
    # the tags from the comma-based peeling and pinning below.
    scene = re.sub(rf"\b({_COUNT_TOKEN})\.", r"\1,", scene, flags=re.IGNORECASE)
    # Tags and hybrid are comma-delimited by contract, so a chunk is one tag or one
    # bound clause and dropping it stays surgical. Their encoders draw "no longer
    # wearing X" as X, a booru-trained composer writes "pov" unprompted, and
    # "camera" puts a literal one in the frame.
    scene = strip_chunks(scene, _NEGATION_CHUNK_RE, whole=False)
    scene = strip_chunks(scene, _POV_CHUNK_RE)
    scene = strip_chunks(scene, _CAMERA_CHUNK_RE, whole=False)
    # Only first-person has a viewer to touch.
    if pov == FIRST:
        scene = rewrite_viewer_contact(scene)
    return scene
