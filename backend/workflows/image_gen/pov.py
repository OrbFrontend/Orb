"""Resolve the image camera to exactly one viewpoint, before any LLM call.

POV used to be the prompter model's guess -- a soft sentence in the OOC tail plus
a `viewpoint` enum on `analyze_scene`. That left every prompt hedging for both
cases and the analyzer spending a decision on the question it is worst at. Here
the camera is decided first, from four levers, so each mode gets instructions
written for it alone.

The levers, first hit wins:

1. a camera tag in the character's fixed appearance prompt -- an explicit user pin
2. a manual per-conversation ``pov_mode`` of "first" or "third"
3. the local povtense classifier, walking back through recent assistant messages
   while it answers "ambiguous"
4. ``DEFAULT_POV``

Nothing here is persisted. "Use the previous message's POV" is served by
re-classifying the actual previous messages (lever 3), which beats a cached value
and keeps the whole generation path free of ``workflow_state`` writes -- the
trigger route holds ``workflow_state_lock`` around the on-demand hook but releases
it before a streaming response body runs, and ``asyncio.Lock`` is not reentrant.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import get_settings, local_ml

logger = logging.getLogger(__name__)

FIRST = "first_person"
THIRD = "third_person"

# What the user may choose per conversation. "auto" runs the classifier and
# degrades to DEFAULT_POV when it is not installed or is toggled off.
POV_MODES = ("auto", "first", "third")
DEFAULT_MODE = "auto"
DEFAULT_POV = THIRD

_MANUAL = {"first": FIRST, "third": THIRD}

# First- and second-person narration are the same camera: both put it behind the
# user's eyes. Only third-person narration watches from outside.
_CLASSIFIER_POV = {"first": FIRST, "second": FIRST, "third": THIRD}

# How many assistant messages back the classifier may walk while it keeps
# answering "ambiguous". Deep enough to cross a couple of terse replies, shallow
# enough that it cannot inherit the POV of a scene that has since changed.
LOOKBACK = 3

FEATURE = "pov_classifier"

# A camera tag in the profile's fixed tags is an explicit user choice, but the
# prompter never sees it as one: that block is handed over as appearance tags it
# must not repeat, so a 'third_person' sitting in it reads as a trait, not a
# camera. Parse it here and let it win outright.
_VIEWPOINT_TAG_RE = re.compile(
    r"\b(?:(?P<first>(?:first|1st)[ _-]?person|pov)|(?P<third>(?:third|3rd)[ _-]?person))\b",
    re.IGNORECASE,
)


def normalize_mode(value: Any) -> str:
    """The stored ``pov_mode``, or the default for anything unrecognized."""
    return value if value in POV_MODES else DEFAULT_MODE


def pinned_viewpoint(appearance: Any) -> str | None:
    """The viewpoint the fixed appearance tags demand, or None when they name none."""
    if not isinstance(appearance, str):
        return None
    match = _VIEWPOINT_TAG_RE.search(appearance)
    if match is None:
        return None
    return FIRST if match.group("first") else THIRD


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
    appearance: str = "",
    mode: str = DEFAULT_MODE,
    history: Sequence[Mapping[str, Any]] = (),
) -> tuple[str, str]:
    """The camera for one generation, as ``(viewpoint, source)``.

    *source* is recorded on the attachment so a wrong camera can be traced to the
    lever that chose it rather than guessed at.
    """
    pinned = pinned_viewpoint(appearance)
    if pinned is not None:
        return pinned, "character_tag"
    manual = _MANUAL.get(normalize_mode(mode))
    if manual is not None:
        return manual, "manual"
    if await classifier_ready():
        classified = await _classify(history)
        if classified is not None:
            return classified, "classifier"
    return DEFAULT_POV, "default"
