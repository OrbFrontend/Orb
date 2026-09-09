"""Classify and compare the narrative voice of recent messages.

The markup half of this workflow is fully algorithmic; a narrator is not, so the
voice half reads the host's 17M-param POV/tense classifier instead. Every text
is parsed under *its own* markup convention: ``classify_axes`` answers "how is
this message formatted?", and its dialogue axis is what ``narration_only`` needs
to hand the classifier narration rather than speech. The window's aggregate
convention answers a different question -- "what should the reply look like?" --
and belongs to the rewrite target alone. Reading a message under the aggregate
is how a quoted draft in a bare-dialogue conversation ends up classified from an
empty string, which reports ambiguous and hides the very drift this half exists
to find.

The classifier's labels are strings, with ``"ambiguous"`` serving as the unknown
sentinel the agreement rule in ``stable_label`` takes.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from typing import Any

from ..toolkit import (
    AxisStyle,
    classify_axes,
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


async def _classify_narration(text: str, style: AxisStyle) -> VoiceLabels | None:
    """Classify the narration *text* exposes under its own markup *style*."""
    try:
        return await classify_pov_tense(narration_only(text, style.dialogue))
    except Exception as e:
        logger.warning("[format_consistency] POV/tense classification failed (%r); skipping the voice check", e)
        return None


async def classify(text: str) -> VoiceLabels | None:
    """Return the narration's (POV, tense), or ``None`` on a local-ML fault.

    Which spans are speech is read from *text*, not from the window: bare
    dialogue leaves speech unmarked, quoted dialogue marks it, and applying the
    wrong one either feeds unmarked speech to the classifier or feeds it nothing
    at all.
    """
    return await _classify_narration(text, classify_axes(text))


async def labels_for(msg: Mapping[str, Any]) -> VoiceLabels | None:
    """The cached narration labels for a history row, classifying on a miss.

    The content digest and the message's *own* dialogue convention form the cache
    identity, so an edit invalidates the row without coupling message routes to
    this workflow -- and a row stays valid when the window around it changes,
    because nothing about the window went into it. (The convention is recorded
    even though the digest already implies it: a build whose ``classify_axes``
    answers differently must not keep serving labels taken from the old answer.)

    The bridge already holds the owning conversation lock.
    """
    text = msg.get("content") or ""
    if not isinstance(text, str):
        return UNKNOWN_LABELS
    style = classify_axes(text)
    mid = msg.get("id")
    if not isinstance(mid, int):
        return await _classify_narration(text, style)
    digest = _content_digest(text)
    cached = await get_workflow_message_state(mid, WORKFLOW_ID)
    if isinstance(cached, Mapping):
        pov, tense = cached.get("pov"), cached.get("tense")
        if (
            cached.get("content_sha256") == digest
            and cached.get("dialogue") == style.dialogue.value
            and isinstance(pov, str)
            and isinstance(tense, str)
        ):
            return pov, tense
    labels = await _classify_narration(text, style)
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
