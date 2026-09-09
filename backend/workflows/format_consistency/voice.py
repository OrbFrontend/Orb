"""Classify and compare the narrative voice of recent messages.

The markup half of this workflow is fully algorithmic; a narrator is not, so the
voice half reads the host's 17M-param POV/tense classifier instead. The two share
exactly one thing -- ``stable_label``, the rule that decides whether the window
agreed -- and nothing else: the classifier's labels are already strings, with
``"ambiguous"`` serving as the unknown sentinel that rule takes.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from ..toolkit import (
    classify_pov_tense,
    get_workflow_message_state,
    local_feature_available,
    set_workflow_message_state,
    stable_label,
)
from . import WORKFLOW_ID

logger = logging.getLogger(__name__)

FEATURE = "pov_classifier"

# The classifier's own sentinel: a class it was trained to emit, and the value
# ``stable_label`` treats as "no opinion" on either end of a comparison.
UNKNOWN = "ambiguous"

# Phrases for the rewrite instruction. Only these labels can drift -- UNKNOWN
# never reaches the instruction, because a drift needs both ends confident.
_POV_PHRASE = {"first": "first person", "second": "second person", "third": "third person"}
_TENSE_PHRASE = {"past": "past tense", "present": "present tense"}


def classifier_ready(settings: Mapping[str, Any]) -> bool:
    """Extras installed, model on disk, and the feature toggle left on.

    Sync, unlike image_gen's version: ``local_ml_enabled`` is already decoded by
    ``get_settings()`` and ``ctx.settings`` is that mapping, so the answer is in
    hand and costs no DB round trip inside the turn.
    """
    ok, _reason = local_feature_available(FEATURE)
    if not ok:
        return False
    enabled = settings.get("local_ml_enabled") or {}
    return enabled.get(FEATURE, True) is not False


async def classify(text: str) -> tuple[str, str]:
    """(POV, tense) for one message, or the sentinel pair on any local-ML fault.

    A model that fails to load must cost the consistency check, not the turn --
    the same rule the image_gen camera states.
    """
    try:
        return await classify_pov_tense(text)
    except Exception as e:
        # One line, not a traceback: a load failure is sticky, so every message in
        # the window raises it on the same turn, and the cached reason local_ml
        # carries in the message is the informative part.
        logger.warning("[format_consistency] POV/tense classification failed (%r); skipping the voice check", e)
        return UNKNOWN, UNKNOWN


async def labels_for(msg: Mapping[str, Any]) -> tuple[str, str]:
    """The cached (POV, tense) for a history row, classifying on a miss.

    Backfill-on-read is the only write path: history predating the toggle carries
    no labels either way, so a second write at draft time would save exactly one
    embed per turn and buy nothing else.

    Takes no lock. The bridge already holds ``workflow_state_lock(cid, wid)``
    around this hook, which is the owning-conversation lock message state
    requires, and ``asyncio.Lock`` is not reentrant -- re-entering deadlocks.
    """
    text = msg.get("content") or ""
    if not isinstance(text, str):
        return UNKNOWN, UNKNOWN
    mid = msg.get("id")
    if not isinstance(mid, int):
        # The unit-test fixture shape (and any caller synthesizing rows): nothing
        # to key a cache on, so classify inline and skip the write.
        return await classify(text)
    cached = await get_workflow_message_state(mid, WORKFLOW_ID)
    if isinstance(cached, Mapping):
        pov, tense = cached.get("pov"), cached.get("tense")
        if isinstance(pov, str) and isinstance(tense, str):
            return pov, tense
    pov, tense = await classify(text)
    payload = dict(cached) if isinstance(cached, Mapping) else {}
    payload.update({"pov": pov, "tense": tense})
    await set_workflow_message_state(mid, WORKFLOW_ID, payload)
    return pov, tense


def target(window_labels: list[tuple[str, str]]) -> tuple[str, str]:
    """The (POV, tense) the draft is held to, each axis UNKNOWN unless the window
    agrees on it. The two axes are voted independently: a window may have settled
    its tense while its POV is still moving."""
    return (
        stable_label([pov for pov, _ in window_labels], UNKNOWN),
        stable_label([tense for _, tense in window_labels], UNKNOWN),
    )


def drift(source: tuple[str, str], target_labels: tuple[str, str]) -> list[str]:
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
