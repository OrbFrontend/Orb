"""Resolve the image camera to exactly one viewpoint, before any LLM call, so each
mode gets instructions written for it alone and the analyzer never spends a
decision on the question it is worst at.

The levers, first hit wins:

1. a manual ``pov_mode`` of "first" or "third", from the global workflow config
2. the local povtense classifier, walking back through recent assistant messages
   while it answers "ambiguous"
3. ``DEFAULT_POV``

A camera tag in the character's appearance prompt is not a lever -- the picker owns
the camera, so the tag stays plain appearance data. Nothing here is persisted:
"use the previous message's POV" is served by re-classifying it (lever 2).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import get_settings, local_ml

logger = logging.getLogger(__name__)

FIRST = "first_person"
THIRD = "third_person"

POV_MODES = ("auto", "first", "third")
DEFAULT_MODE = "auto"
DEFAULT_POV = THIRD

_MANUAL = {"first": FIRST, "third": THIRD}

DEFAULT_POV_MODE = next(mode for mode, viewpoint in _MANUAL.items() if viewpoint == DEFAULT_POV)

_CLASSIFIER_POV = {"first": FIRST, "second": FIRST, "third": THIRD}

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
    """Assistant message bodies, newest first, capped at LOOKBACK. Only assistant
    turns: the camera describes the reply being illustrated, and the user's own turn
    is written from a persona voice that need not match it."""
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

    None when every candidate is ambiguous, there is nothing to read, or the model
    fails to load -- the caller falls through rather than treating a local-ML
    problem as a generation failure.
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
    if not await classifier_ready():
        return DEFAULT_POV, "no_classifier"
    classified = await _classify(history)
    if classified is not None:
        return classified, "classifier"
    return DEFAULT_POV, "default"
