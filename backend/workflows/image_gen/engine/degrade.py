"""Choose bounded fallbacks when a provider rejects optional fields."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .contracts import (
    ImageGenerationError,
    ResolvedReference,
    fold_seed_into,
    ratio_distance,
)

# The failure kind that means "the provider read the request and would not take it".
# `auth` and `server` are about something other than what we sent, and a model that is
# simply gone is the adapter's own retry, not this one.
REQUEST_REFUSED = "request"

# The one failure that is not about the request and is still worth another call: the
# provider asking to be asked more slowly. Named beside its opposite because the ladder
# reads both kinds, and answering this one by *degrading* would give up a reference or
# a resolution to fix something that was never about either.
RATE_LIMITED = "rate_limit"

# The domain word, in the spellings a JSON error body uses. Matched against the
# message rather than any error code, because the codes are the part that differs
# per provider: `IMAGE_INPUT_TOO_MANY`, `missing_image_input` and
# "Unsupported use of 'image_url' parameter" are three providers saying one thing.
_IMAGE_WORDS = ("image", "images", "image_url", "imagedataurls", "reference")

_INTEGERS = re.compile(r"\d+")

# What a refusal has to say before Orb will refit the seed and ask again.
#
# The seed is the one thing in a request that Orb chose arbitrarily, so a provider
# that names its own bound has said everything needed to answer -- there is no
# `/object_info` out here to declare it in advance, and a `max_seed` column on the
# preset table would be the per-model allowlist this module exists to avoid. It is
# also genuinely per-model: Together AI reaches its seed check only after resolving
# the model, and two of its models answered differently about everything else.
#
# Measured on Together AI 2026-09-06: *"Invalid value for 'seed' parameter. Seed must
# be an integer value between 0 and 2147483647."* -- inclusive at both ends, verified
# by watching 0 and 2147483647 reach the next check while -1 and 2147483648 did not.
_SEED_BETWEEN = re.compile(r"between\s+(-?\d+)\s+and\s+(-?\d+)")

# The same fact in the other spelling a machine-generated validator uses. Ordered so
# the inclusive phrasings match before `less than`, whose bound is one lower.
_SEED_CEILING = re.compile(
    r"(?:less than or equal to|at most|no (?:greater|larger|more) than|maximum(?: of)?"
    r"|(?P<exclusive>less than|below|under))\s+(?P<bound>-?\d+)"
)

# Both bounds are anchored on the words around the number, never on a bare integer:
# the message carries Orb's own "(HTTP 400)" prefix, and reading a status code as a
# seed bound would fold a perfectly good seed into [0, 400] and render the wrong
# picture rather than fail.
_SEED_TOKEN = re.compile(r"(?<![a-z0-9_])seed(?![a-z0-9_])")

# The pixel-area window a refusal quotes, as two `AxB` corners bounding a range.
# Measured on Together AI 2026-09-06: *"Invalid dimensions. The total area (width ×
# height) must be within the range of 1265×1265 to 1440×1440. The aspect ratio must
# be between 1:4 and 4:1."* -- which refuses a perfectly ordinary 1024x1536 request
# at 1,572,864 px, 1.7% under the floor.
#
# An *area* window is not something the preset grid can express: `min_dimension`,
# `max_dimension` and `dimension_step` bound each edge on its own, and every edge here
# is legal. Nor is it a provider fact -- it belongs to the model, which is exactly the
# per-model table this module refuses to keep. So the model states it, in the refusal.
#
# Both separators are live: the message writes U+00D7 between the edges of a corner
# and the plain letter elsewhere.
_AREA_RANGE = re.compile(r"(\d+)\s*[x×*]\s*(\d+)\s*(?:to|and|-|–)\s*(\d+)\s*[x×*]\s*(\d+)")

# Every `AxB` pair a refusal lists, for the *other* way a model says "not that size":
# a menu. One Together model quotes the area window above; another answers *"Supported
# values are: '1024x1024', '1264x848', ... '384x3072'."* -- fourteen fixed sizes. Same
# provider, same endpoint, same day, two incompatible grammars. Which is the argument
# for reading it here rather than keeping a column: a column would have to be right
# about every model in a catalogue that grows without us, and these two disagree.
_SIZE_PAIR = re.compile(r"(?<![\d.])(\d{2,5})\s*[x×*]\s*(\d{2,5})(?![\d.])")

# Anchored the way the seed bounds are, and for the same reason: without this, a
# refusal that merely happened to contain two `AxB` pairs would resize the render.
_SIZE_WORDS = ("dimension", "area", "resolution", "width", "height", "pixel", "size")

# Beyond this, an integer in a refusal is a byte count, a pixel bound or an id --
# never a number of reference images. Bounded rather than clever: the point is to
# refuse to read "10485760" as a slot count, not to parse English.
_PLAUSIBLE_COUNT = 64

# The optional `ImageRequest` fields a render can stop sending and still be the render
# that was asked for, mapped to what the user is told when one goes. **Not a capability
# table**: nothing here says which model takes what -- the model says that itself, in
# the refusal -- this is only what Orb is willing to give up in answer, and the wording
# for having given it up.
#
# Two things put a field here, and both keep the list short on purpose. It has to clear
# to a value the request builders already omit, so the step is `replace(request,
# field="")` and no absent-sentinel is invented -- which is why `seed` is not here:
# `ImageRequest.seed` is an `int` a ComfyUI graph needs structurally, so a provider
# that refuses one is answered by `_refit_seed` instead, which changes the number
# rather than dropping the field. And its name has to be unmistakable in a sentence,
# so that matching it cannot misread prose -- `negative_prompt` can only be a provider
# naming the parameter, while a refusal that happens to say "quality" may be talking
# about anything at all.
DROPPABLE_FIELDS: dict[str, str] = {
    "negative_prompt": "this model does not take a negative prompt, so it was rendered without one",
}

# Matched on the whole token, never a substring: `negative_prompt` inside
# `default_negative_prompt` is a different field, and the point of the rule above is
# that a match is unambiguous.
_FIELD_TOKENS = {name: re.compile(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])") for name in DROPPABLE_FIELDS}


# What may be remembered about a render target between calls, and nothing else.
#
# `seed_high`/`seed_low` bound the seed; `sizes` maps one requested `WxH` to the `WxH`
# the target actually rendered. Both are **bounds**: replaying one can only produce a
# request the provider already said it would take. Deliberately absent is anything of
# the form "this model does not support X" -- that is a capability, it goes stale in
# the direction that silently withholds what the user paid for, and the refusal is
# there to answer it fresh every time.
#
# Nothing here is keyed by a model *in the codebase*. The keys are written at runtime
# from what a provider said about a model nobody enumerated.
LEARNABLE = ("seed_high", "seed_low", "sizes")


@dataclass(frozen=True)
class Rung:
    """One step down: what the next attempt sends, and what the user is told about it.

    `keep` is how many references it may carry; `drop` names an optional request field
    it stops sending; `seed` replaces the seed and `size` the resolution. A rung changes
    exactly one of those -- when anything else is refit, `keep` is left at what was just
    sent, because the references were not what was refused.

    `note` is empty on a seed refit alone, which is the one step that costs the render
    nothing: the picture is still the one asked for, and the number that reproduces it
    is recorded on the attachment. A note here would fire on every render against a
    provider whose bound is narrower than Orb's seed, which is how the disclosures
    that do matter stop being read.

    `learned` is what this refusal revealed about the target, in the shape
    `LEARNABLE` describes -- **bounds, never capabilities**. A stored bound can only
    make the next request valid; a stored *capability* would decide a feature is
    unavailable and withhold something the user configured and is paying for, with
    nothing on screen to say so. The first is worth remembering and the second is the
    table this module exists to not keep.
    """

    keep: int
    note: str = ""
    drop: str = ""
    seed: int | None = None
    size: tuple[int, int] | None = None
    learned: Mapping[str, Any] = field(default_factory=dict)


def _mentions_image(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in _IMAGE_WORDS)


def _named_field(message: str, sending: Sequence[str]) -> str:
    """The droppable field this refusal names, if it names one that was actually sent.

    `sending` is read off the attempt rather than declared, so a field already given up
    on an earlier rung is no longer a candidate and the ladder cannot walk in place.
    """
    lowered = message.lower()
    return next((name for name in sending if name in _FIELD_TOKENS and _FIELD_TOKENS[name].search(lowered)), "")


def _named_limit(message: str, *, sent: int) -> int | None:
    """The reference count this refusal names, if it names one.

    Providers that enforce a limit tend to quote it -- *"This model accepts up to 3
    input images"* -- and that is worth far more than a guess, because it lands on
    the answer in one retry instead of collapsing to zero references.

    Only integers *below* what was just sent are candidates: a message quoting the
    number we sent is describing the problem, not the remedy. The largest surviving
    candidate wins, being the most references the provider has agreed to.
    """
    candidates = [
        value for raw in _INTEGERS.findall(message) for value in (int(raw),) if 0 < value < sent and value <= _PLAUSIBLE_COUNT
    ]
    return max(candidates) if candidates else None


def _size_key(size: tuple[int, int]) -> str:
    return f"{size[0]}x{size[1]}"


def resized_note(size: tuple[int, int], fitted: tuple[int, int]) -> str:
    """The one disclosure both routes to a resize share -- the ladder's, and a replay
    of what the ladder learned earlier. Shared so a remembered resize cannot describe
    itself differently from the refusal that first discovered it."""
    return f"this model does not render at {_size_key(size)}, so it was rendered at {_size_key(fitted)}"


def _seed_range(message: str) -> tuple[int, int] | None:
    """The seed range this refusal names, if it names one, as an inclusive pair."""
    pair = _SEED_BETWEEN.search(message)
    if pair:
        low, high = int(pair.group(1)), int(pair.group(2))
        return (low, high) if low <= high else None
    ceiling = _SEED_CEILING.search(message)
    if ceiling is None:
        return None
    high = int(ceiling.group("bound"))
    # No lower bound is ever quoted alongside a ceiling, and zero is the floor every
    # measured backend accepts -- ComfyUI's own nodes included, via `fit_seed`.
    return (0, high - 1 if ceiling.group("exclusive") else high)


def _refit_seed(message: str, seed: int) -> int | None:
    """`seed` folded into the range this refusal names, or None to leave it alone.

    None when the refusal does not name the seed, when it names no range, and -- the
    one that keeps the ladder honest -- when the fold changes nothing. A refusal
    quoting a range the seed is already inside is talking about something else, and a
    rung that resent the same number would spend an attempt to be told the same thing.
    """
    if not _SEED_TOKEN.search(message.lower()):
        return None
    bounds = _seed_range(message)
    if bounds is None:
        return None
    folded = fold_seed_into(seed, *bounds)
    return folded if folded != seed else None


def _refit_size(message: str, size: tuple[int, int]) -> tuple[int, int] | None:
    """`size` refit to whatever this refusal says it will render, or None to leave it.

    Two grammars, because providers use both: a pixel-**area window**, which is
    answered by rescaling, and a **menu** of fixed sizes, which is answered by picking
    the nearest. Nothing here knows which model speaks which -- the refusal does.

    The aspect ratio survives: both edges move by one factor, the way `pixels_for`
    scales an over-large request. The composition is what the user actually chose,
    and the pixel count is the part they are unlikely to have meant precisely.

    Aimed at the **geometric middle** of the window rather than at the nearer bound.
    The request is snapped to the provider's own step grid afterwards, on the way back
    through `pixels_for`, and a size fitted flush against a bound can be snapped
    straight back out of it -- refused a second time, with the refit no longer able to
    change anything and so no rung left to answer. The middle is the point furthest
    from both bounds in the multiplicative sense that a rescale moves in.

    None when the area is already inside the window, which is how an *aspect* refusal
    -- the other half of the sentence Together quotes -- declines to be answered here:
    rescaling cannot change an aspect ratio, so the refusal is left to stand and say so
    in the user's own words.
    """
    lowered = message.lower()
    if not any(word in lowered for word in _SIZE_WORDS):
        return None
    window = _AREA_RANGE.search(message)
    if window is not None:
        # A range wins outright, and its verdict is final even when it declines. The
        # message that quotes a window also quotes example corners, so falling through
        # to the menu reader would offer those corners as though they were a menu.
        return _rescaled(size, int(window.group(1)) * int(window.group(2)), int(window.group(3)) * int(window.group(4)))
    return _nearest_offered(message, size)


def _rescaled(size: tuple[int, int], low: int, high: int) -> tuple[int, int] | None:
    """`size` scaled into the pixel-area window `[low, high]`, or None to leave it."""
    width, height = size
    area = width * height
    if low > high or area <= 0 or low <= area <= high:
        return None
    scale = math.sqrt(math.sqrt(low * high) / area)
    fitted = (max(1, round(width * scale)), max(1, round(height * scale)))
    return fitted if fitted != size else None


def _nearest_offered(message: str, size: tuple[int, int]) -> tuple[int, int] | None:
    """The size nearest `size` among those this refusal lists, or None if it lists none.

    Ranked exactly as `size_for` ranks a menu declared on a preset -- aspect ratio
    first, in log space, then total pixels -- so a menu learned from a refusal and one
    read off the preset table land on the same answer. A model that states its sizes
    only when refused must not get a worse pick than one that published them.

    **Two offers minimum.** A lone pair in a refusal is as likely to be the request
    being quoted back at us, or a maximum upload edge, as it is an offer; a menu is
    plural by nature. This is what keeps the reader off messages that merely mention a
    resolution in passing.
    """
    offered = [(int(w), int(h)) for w, h in _SIZE_PAIR.findall(message)]
    choices = [(w, h) for w, h in offered if w > 0 and h > 0]
    if len(choices) < 2 or size[1] <= 0 or size in choices:
        # `size in choices` is the same guard the seed and the window use: a menu that
        # already contains what was sent is a refusal about something else, and moving
        # to a *different* offered size would answer a question nobody asked.
        return None
    target = size[0] / size[1]
    return min(choices, key=lambda c: (ratio_distance(target, c[0] / c[1]), abs(c[0] * c[1] - size[0] * size[1])))


def next_rung(
    exc: Exception,
    *,
    sent: int,
    droppable: int,
    seed: int = 0,
    size: tuple[int, int] | None = None,
    sending: Sequence[str] = (),
) -> Rung | None:
    """The next degradation to try, or None to let the failure stand.

    `droppable` is how many of the references may be left out at all. A ComfyUI
    graph's image inputs are `required` -- rendering one unfilled submits whatever
    filename the workflow was exported with -- so a graph degrades to nothing and
    the error is raised untouched. No backend is named here to arrange that; the
    slots already carry the fact.

    `sending` is the droppable optional fields this attempt is carrying, in the sense
    of `DROPPABLE_FIELDS`. `seed` and `size` are what this attempt sent, for a provider
    that refuses either and quotes the range it wanted instead.
    """
    if not isinstance(exc, ImageGenerationError) or getattr(exc, "kind", "") != REQUEST_REFUSED:
        return None
    message = str(exc)
    # Asked first, because a refit is the only rung that costs the render nothing. The
    # seed is a number Orb drew at random, so folding it into the range the provider
    # named gives back the picture the user actually asked for, while every rung below
    # gives up something they configured. It cannot race the rungs below either: a
    # refusal has to name `seed` *and* quote a range the current seed falls outside.
    refit = _refit_seed(message, seed)
    if refit is not None:
        bounds = _seed_range(message) or (0, refit)
        return Rung(keep=sent, seed=refit, learned={"seed_low": bounds[0], "seed_high": bounds[1]})
    # Before the references, because a size refusal is free to mention an "image" and
    # would otherwise cost the user a likeness to answer a complaint about pixels.
    # Unlike the seed this one is disclosed: the resolution is a setting they chose.
    fitted = _refit_size(message, size) if size is not None else None
    if fitted is not None and size is not None:
        return Rung(
            keep=sent,
            note=resized_note(size, fitted),
            size=fitted,
            # The mapping, not the window or the menu it came from: a style renders at
            # one size over and over, so remembering the answer for the size actually
            # asked for is both smaller and exactly right, and it cannot be wrong the
            # way a re-derived constraint can.
            learned={"sizes": {_size_key(size): _size_key(fitted)}},
        )
    # Asked before the references, and before the reference guards: a refusal that names
    # one of Orb's own fields is the most specific thing a provider can say, and it is
    # answerable on a render that carries no references at all -- which is exactly the
    # attempt a reference rung has just left behind. The two can never both claim one
    # refusal, because no reference field is a `DROPPABLE_FIELDS` name.
    named = _named_field(message, sending)
    if named:
        return Rung(keep=sent, note=DROPPABLE_FIELDS[named], drop=named)
    if sent <= 0 or droppable <= 0:
        return None
    if not _mentions_image(message):
        # A refusal about the size, the prompt length, or a parameter Orb may not drop.
        # Dropping a likeness would not fix it and would cost the user the thing they
        # actually asked for.
        return None
    limit = _named_limit(message, sent=sent)
    if limit is not None and limit >= sent - droppable:
        dropped = sent - limit
        return Rung(
            keep=limit,
            note=(
                f"this model accepts {limit} reference image{'' if limit == 1 else 's'}, "
                f"so {dropped} of the {sent} sent {'was' if dropped == 1 else 'were'} left out"
            ),
        )
    keep = sent - droppable
    return Rung(
        keep=keep,
        note=(
            "this model would not take the reference images, so it was rendered from the prompt alone"
            if keep == 0
            else f"this model would not take {sent - keep} of the reference images, so they were left out"
        ),
    )


def trim(references: Sequence[ResolvedReference], optional: Sequence[bool], keep: int) -> tuple[ResolvedReference, ...]:
    """`references` reduced to `keep` in total, dropping only what may be dropped.

    `keep` counts every reference, not only the optional ones, because that is what
    the provider's refusal talks about. Anything not droppable survives regardless
    and is charged against the budget first.

    Drops from the **end**, because the list is positional everywhere else in this
    workflow: subject 0 is the render's primary and the one a solo slot must always
    resolve, so the last cast member is the right thing to lose first.
    """
    budget = max(0, keep - sum(1 for is_optional in optional if not is_optional))
    surviving: list[ResolvedReference] = []
    for reference, is_optional in zip(references, optional):
        if not is_optional:
            surviving.append(reference)
            continue
        if budget > 0:
            surviving.append(reference)
            budget -= 1
    return tuple(surviving)
