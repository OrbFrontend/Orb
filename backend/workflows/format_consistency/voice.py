"""Classify and compare recent messages' narrative voice."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from ..toolkit import (
    AxisStyle,
    classify_pov_tense,
    get_workflow_message_state,
    local_model_identity,
    narration_only,
    set_workflow_message_state,
    stable_label,
)
from . import WORKFLOW_ID

logger = logging.getLogger(__name__)

FEATURE = "pov_classifier"

UNKNOWN = "ambiguous"
VoiceLabels = tuple[str, str]
UNKNOWN_LABELS: VoiceLabels = (UNKNOWN, UNKNOWN)

_POV_PHRASE = {"first": "first person", "second": "second person", "third": "third person"}
_TENSE_PHRASE = {"past": "past tense", "present": "present tense"}


def _content_digest(text: str) -> str:
    """Identify the message and the narration-extraction policy that labeled it."""
    return hashlib.sha256(b"narration-v2\0" + text.encode()).hexdigest()


async def classify(text: str, style: AxisStyle) -> VoiceLabels | None:
    """Return the (POV, tense) of *text*'s narration, extracted under its markup
    reading *style*, or ``None`` on a local-ML fault."""
    try:
        return await classify_pov_tense(narration_only(text, style.dialogue))
    except Exception as e:
        logger.warning("[format_consistency] POV/tense classification failed (%r); skipping the voice check", e)
        return None


async def labels_for(msg: Mapping[str, Any], style: AxisStyle) -> VoiceLabels | None:
    """Return cached labels for a history row, classifying on a miss.

    *style* is the row's markup reading. Its dialogue decides which spans count as
    narration, so a cached row is reused only under the same dialogue reading.
    """
    text = msg.get("content") or ""
    if not isinstance(text, str):
        return UNKNOWN_LABELS
    mid = msg.get("id")
    if not isinstance(mid, int):
        return await classify(text, style)
    digest = _content_digest(text)
    classifier = local_model_identity(FEATURE)
    cached = await get_workflow_message_state(mid, WORKFLOW_ID)
    if isinstance(cached, Mapping):
        pov, tense = cached.get("pov"), cached.get("tense")
        if (
            cached.get("content_sha256") == digest
            and cached.get("classifier") == classifier
            and cached.get("dialogue") == style.dialogue.value
            and isinstance(pov, str)
            and isinstance(tense, str)
        ):
            return pov, tense
    labels = await classify(text, style)
    if labels is None:
        return None
    pov, tense = labels
    payload = dict(cached) if isinstance(cached, Mapping) else {}
    payload.update(
        {
            "pov": pov,
            "tense": tense,
            "dialogue": style.dialogue.value,
            "content_sha256": digest,
            "classifier": classifier,
        }
    )
    await set_workflow_message_state(mid, WORKFLOW_ID, payload)
    return labels


def target(window_labels: list[VoiceLabels]) -> VoiceLabels:
    """Return independently voted target (POV, tense) labels."""
    return (
        stable_label([pov for pov, _ in window_labels], UNKNOWN),
        stable_label([tense for _, tense in window_labels], UNKNOWN),
    )


def drift(source: VoiceLabels, target_labels: VoiceLabels) -> list[str]:
    """Return human phrases for confident voice axes that drifted."""
    phrases: list[str] = []
    for src, tgt, table in (
        (source[0], target_labels[0], _POV_PHRASE),
        (source[1], target_labels[1], _TENSE_PHRASE),
    ):
        if src != UNKNOWN and tgt != UNKNOWN and src != tgt and tgt in table:
            phrases.append(table[tgt])
    return phrases
