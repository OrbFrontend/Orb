"""What to do when a provider refuses what it was sent.

The alternative this replaces was a table: a per-provider count of how many
references are read, and a per-provider allowlist of which models read any. Both
were hand-measured against catalogues that grow without us -- NanoGPT alone ships
202 image models -- so both were permanently unfinished, and an unfinished
allowlist withholds a capability the user configured and is paying for, silently.

**Asking is cheaper than tabulating.** Every provider measured refuses a bad
request *before* rendering, and bills nothing for the refusal: an over-capacity
reference array, an unknown model and an invalid enum all came back 400/404/422
for free. So the render sends what the style asked for, and a provider that cannot
take it says so at no cost.

**Classified on the shape of the refusal, never on a provider's vocabulary.** The
same posture `pipeline/failures.py` takes for turn errors. Two facts decide it:
the failure was about the *request* rather than the credential or the server, and
the message is about images. Anything else is not a reference problem, and
degrading on it would quietly drop a likeness because the resolution was wrong.

**Bounded, and disclosed.** At most two rungs -- the count the provider named, then
none at all -- so a render costs at most three attempts, and each rung it takes
appends a note. Silent degradation is the thing this exists to avoid; it is only
worth doing because the user is told.
"""

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


@dataclass(frozen=True)
class Rung:
    """One step down: how many references the next attempt may carry, and what the
    user is told about the step."""

    keep: int
    note: str


def _mentions_image(message: str) -> bool:
    lowered = message.lower()
    return any(word in lowered for word in _IMAGE_WORDS)


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


def next_rung(exc: Exception, *, sent: int, droppable: int) -> Rung | None:
    """The next degradation to try, or None to let the failure stand.

    `droppable` is how many of the references may be left out at all. A ComfyUI
    graph's image inputs are `required` -- rendering one unfilled submits whatever
    filename the workflow was exported with -- so a graph degrades to nothing and
    the error is raised untouched. No backend is named here to arrange that; the
    slots already carry the fact.
    """
    if not isinstance(exc, ImageGenerationError) or getattr(exc, "kind", "") != REQUEST_REFUSED:
        return None
    if sent <= 0 or droppable <= 0:
        return None
    message = str(exc)
    if not _mentions_image(message):
        # A refusal about the size, the prompt length or a parameter name. Dropping a
        # likeness would not fix it and would cost the user the thing they asked for.
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
