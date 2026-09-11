"""Resolve the image prompt's point of view."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import (
    classify_pov,
    get_settings,
    local_feature_ready,
    markup_axes,
    narration_only,
)

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
    return local_feature_ready(FEATURE, await get_settings())


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


async def _classify(history: Sequence[Mapping[str, Any]], settings: Mapping[str, Any]) -> str | None:
    """Walk back over recent assistant messages until one is not ambiguous.

    None when every candidate is ambiguous, there is nothing to read, or the model
    fails to load -- the caller falls through rather than treating a local-ML
    problem as a generation failure.
    """
    for text in _assistant_texts(history):
        try:
            # Match the voice check: remove bare speech before taking the tail,
            # or a long spoken passage can displace every narration sentence.
            style = await markup_axes(text, settings)
            label = await classify_pov(narration_only(text, style.dialogue))
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
    settings = await get_settings()
    if not local_feature_ready(FEATURE, settings):
        return DEFAULT_POV, "no_classifier"
    classified = await _classify(history, settings)
    if classified is not None:
        return classified, "classifier"
    return DEFAULT_POV, "default"
