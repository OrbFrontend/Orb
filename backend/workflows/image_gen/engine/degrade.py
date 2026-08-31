"""Choose bounded fallbacks when a provider rejects optional fields."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from .contracts import ImageGenerationError, ResolvedReference

# The failure kind that means "the provider read the request and would not take it".
# `auth`, `rate_limit` and `server` are all about something other than what we sent,
# and a model that is simply gone is the adapter's own retry, not this one.
REQUEST_REFUSED = "request"

# The domain word, in the spellings a JSON error body uses. Matched against the
# message rather than any error code, because the codes are the part that differs
# per provider: `IMAGE_INPUT_TOO_MANY`, `missing_image_input` and
# "Unsupported use of 'image_url' parameter" are three providers saying one thing.
_IMAGE_WORDS = ("image", "images", "image_url", "imagedataurls", "reference")

_INTEGERS = re.compile(r"\d+")

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
# `ImageRequest.seed` is an `int` a ComfyUI graph needs structurally, and no measured
# provider refuses one outright. And its name has to be unmistakable in a sentence, so
# that matching it cannot misread prose -- `negative_prompt` can only be a provider
# naming the parameter, while a refusal that happens to say "quality" or "seed" may be
# talking about anything at all.
DROPPABLE_FIELDS: dict[str, str] = {
    "negative_prompt": "this model does not take a negative prompt, so it was rendered without one",
}

# Matched on the whole token, never a substring: `negative_prompt` inside
# `default_negative_prompt` is a different field, and the point of the rule above is
# that a match is unambiguous.
_FIELD_TOKENS = {name: re.compile(rf"(?<![a-z0-9_]){re.escape(name)}(?![a-z0-9_])") for name in DROPPABLE_FIELDS}


@dataclass(frozen=True)
class Rung:
    """One step down: what the next attempt sends, and what the user is told about it.

    `keep` is how many references it may carry; `drop` names an optional request field
    it stops sending. A rung changes one or the other -- when a field is dropped `keep`
    is left at what was just sent, because the references were not what was refused.
    """

    keep: int
    note: str
    drop: str = ""


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


def next_rung(exc: Exception, *, sent: int, droppable: int, sending: Sequence[str] = ()) -> Rung | None:
    """The next degradation to try, or None to let the failure stand.

    `droppable` is how many of the references may be left out at all. A ComfyUI
    graph's image inputs are `required` -- rendering one unfilled submits whatever
    filename the workflow was exported with -- so a graph degrades to nothing and
    the error is raised untouched. No backend is named here to arrange that; the
    slots already carry the fact.

    `sending` is the droppable optional fields this attempt is carrying, in the sense
    of `DROPPABLE_FIELDS`.
    """
    if not isinstance(exc, ImageGenerationError) or getattr(exc, "kind", "") != REQUEST_REFUSED:
        return None
    message = str(exc)
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
