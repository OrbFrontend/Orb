"""Classify and compare the narrative voice of recent messages.

The markup half of this workflow is fully algorithmic; a narrator is not, so the
voice half reads the host's 17M-param POV/tense classifier instead. Both halves
consume one ``AxisStyle`` for the window: ``narration_only`` applies its dialogue
axis before local classification, and ``stable_label`` decides whether the voice
labels agree. The classifier's labels are strings, with ``"ambiguous"`` serving
as the unknown sentinel that agreement rule takes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from ..toolkit import (
    AxisStyle,
    classify_pov_tense,
    get_workflow_message_state,
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

# Phrases for the rewrite instruction. Only these labels can drift -- UNKNOWN
# never reaches the instruction, because a drift needs both ends confident.
_POV_PHRASE = {"first": "first person", "second": "second person", "third": "third person"}
_TENSE_PHRASE = {"past": "past tense", "present": "present tense"}


def _content_digest(text: str) -> str:
    """Stable cache identity for the message body without duplicating it in state."""
    return hashlib.sha256(text.encode()).hexdigest()


async def classify(text: str, convention: AxisStyle) -> VoiceLabels | None:
    """Return the narration's (POV, tense), or ``None`` on a local-ML fault.

    The formatting half has already decided which spans are dialogue. Applying
    that same decision here keeps unmarked speech in a bare-dialogue conversation
    from voting on the narrator's person or tense.
    """
    try:
        return await classify_pov_tense(narration_only(text, convention.dialogue))
    except Exception as e:
        logger.warning("[format_consistency] POV/tense classification failed (%r); skipping the voice check", e)
        return None


async def labels_for(msg: Mapping[str, Any], convention: AxisStyle) -> VoiceLabels | None:
    """The cached narration labels for a history row, classifying on a miss.

    The content digest and dialogue convention form the cache identity, so edits
    and convention changes invalidate themselves without coupling message routes
    to this workflow. The bridge already holds the owning conversation lock.
    """
    text = msg.get("content") or ""
    if not isinstance(text, str):
        return UNKNOWN_LABELS
    mid = msg.get("id")
    if not isinstance(mid, int):
        return await classify(text, convention)
    digest = _content_digest(text)
    cached = await get_workflow_message_state(mid, WORKFLOW_ID)
    if isinstance(cached, Mapping):
        pov, tense = cached.get("pov"), cached.get("tense")
        if (
            cached.get("content_sha256") == digest
            and cached.get("dialogue") == convention.dialogue.value
            and isinstance(pov, str)
            and isinstance(tense, str)
        ):
            return pov, tense
    labels = await classify(text, convention)
    if labels is None:
        return None
    pov, tense = labels
    payload = dict(cached) if isinstance(cached, Mapping) else {}
    payload.update(
        {
            "pov": pov,
            "tense": tense,
            "dialogue": convention.dialogue.value,
            "content_sha256": digest,
        }
    )
    await set_workflow_message_state(mid, WORKFLOW_ID, payload)
    return labels


def target(window_labels: list[VoiceLabels]) -> VoiceLabels:
    """The (POV, tense) the draft is held to, each axis UNKNOWN unless the window
    agrees on it. The two axes are voted independently: a window may have settled
    its tense while its POV is still moving."""
    return (
        stable_label([pov for pov, _ in window_labels], UNKNOWN),
        stable_label([tense for _, tense in window_labels], UNKNOWN),
    )


def drift(source: VoiceLabels, target_labels: VoiceLabels) -> list[str]:
    """Human phrases for the axes that must change, empty when consistent.

    An axis counts as drifted only when both ends are confident and differ: an
    UNKNOWN baseline has nothing to enforce, and an UNKNOWN draft (an all-dialogue
    reply, say) gives nothing to enforce it against.
    """
    phrases: list[str] = []
    for src, tgt, table in (
        (source[0], target_labels[0], _POV_PHRASE),
        (source[1], target_labels[1], _TENSE_PHRASE),
    ):
        if src != UNKNOWN and tgt != UNKNOWN and src != tgt and tgt in table:
            phrases.append(table[tgt])
    return phrases
