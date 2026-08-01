"""Resolve the image camera to exactly one viewpoint, before any LLM call.

Three levers decide it, so each mode gets instructions written for it alone -- no prompt hedges for both cases, and the
analyzer never spends a decision on the question it is worst at.

The levers, first hit wins:

1. a manual ``pov_mode`` of "first" or "third", from the global workflow config
2. the local povtense classifier, walking back through recent assistant messages
   while it answers "ambiguous"
3. ``DEFAULT_POV``

A camera tag in the character's appearance prompt is not a lever -- the picker
owns the camera, so the tag stays plain appearance data.

Nothing here is persisted. "Use the previous message's POV" is served by
re-classifying the actual previous messages (lever 2), which beats a cached value.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import get_settings, local_ml

logger = logging.getLogger(__name__)

FIRST = "first_person"
THIRD = "third_person"

# What the user may choose, globally. "auto" runs the classifier and
# degrades to DEFAULT_POV when it is not installed or is toggled off. The picker
# hides "auto" in that state -- it would draw the same camera as the fallback --
# but the mode stays valid here: a config set to auto before the classifier went
# away keeps it, and gets it back when the classifier returns.
POV_MODES = ("auto", "first", "third")
DEFAULT_MODE = "auto"
DEFAULT_POV = THIRD

_MANUAL = {"first": FIRST, "third": THIRD}

# DEFAULT_POV as a picker mode, so the UI can name the camera "Auto" will actually
# produce without keeping its own copy of the default.
DEFAULT_POV_MODE = next(mode for mode, viewpoint in _MANUAL.items() if viewpoint == DEFAULT_POV)

# First- and second-person narration are the same camera: both put it behind the
# user's eyes. Only third-person narration watches from outside.
_CLASSIFIER_POV = {"first": FIRST, "second": FIRST, "third": THIRD}

# How many assistant messages back the classifier may walk while it keeps
# answering "ambiguous". Deep enough to cross a couple of terse replies, shallow
# enough that it cannot inherit the POV of a scene that has since changed.
LOOKBACK = 3

FEATURE = "pov_classifier"


def normalize_mode(value: Any) -> str:
    """The stored ``pov_mode``, or the default for anything unrecognized."""
    return value if value in POV_MODES else DEFAULT_MODE


async def classifier_ready() -> bool:
    """Extras installed, model on disk, and the feature toggle left on."""
    ok, _reason = local_ml.available(FEATURE)
    if not ok:
        return False
    settings = await get_settings()
    return settings.get("local_ml_enabled", {}).get(FEATURE, True) is not False


def _assistant_texts(history: Sequence[Mapping[str, Any]]) -> list[str]:
    """Assistant message bodies, newest first, capped at LOOKBACK.

    Only assistant messages: the camera describes the reply being illustrated, and
    the user's own turn is written from a persona voice that need not match it.
    """
    texts: list[str] = []
    for msg in reversed(history):
        if not isinstance(msg, Mapping) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)
        if len(texts) >= LOOKBACK:
            break
    return texts


async def _classify(history: Sequence[Mapping[str, Any]]) -> str | None:
    """Walk back over recent assistant messages until one is not ambiguous.

    Returns None when every candidate is ambiguous, when there is nothing to read,
    or when the model fails to load -- the caller then falls through to the next
    lever rather than treating a local-ML problem as a generation failure.
    """
    for text in _assistant_texts(history):
        try:
            label = await local_ml.aclassify_pov(text)
        except Exception:
            logger.exception("[image_gen] POV classification failed; falling back")
            return None
        resolved = _CLASSIFIER_POV.get(label)
        if resolved is not None:
            return resolved
    return None


async def resolve(
    *,
    mode: str = DEFAULT_MODE,
    history: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str]:
    """The camera for one generation, as ``(viewpoint, source)``.

    *source* is recorded on the attachment so a wrong camera can be traced to the
    lever that chose it rather than guessed at.
    """
    manual = _MANUAL.get(normalize_mode(mode))
    if manual is not None:
        return manual, "manual"
    # Two different ways "auto" lands on the default, and they are fixed in two
    # different places: install/enable the classifier, or pin the mode by hand.
    if not await classifier_ready():
        return DEFAULT_POV, "no_classifier"
    classified = await _classify(history)
    if classified is not None:
        return classified, "classifier"
    return DEFAULT_POV, "default"
