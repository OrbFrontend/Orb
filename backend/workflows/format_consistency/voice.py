"""Classify and compare recent messages' narrative voice."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from typing import Any

from ..toolkit import (
    AxisStyle,
    classify_pov_tense_chunks,
    get_workflow_message_state,
    local_model_identity,
    narration_only,
    set_workflow_message_state,
)
from .config import WORKFLOW_ID
from .normalization import stable_label

logger = logging.getLogger(__name__)

FEATURE = "pov_classifier"

UNKNOWN = "ambiguous"
#: The addressee's "don't compare": the narration names nobody besides its narrator.
NO_ADDRESSEE = "none"
#: (POV, tense, addressee). The addressee says who else the narration names: "you", someone else ("other"), or nobody.
VoiceLabels = tuple[str, str, str]
UNKNOWN_LABELS: VoiceLabels = (UNKNOWN, UNKNOWN, NO_ADDRESSEE)

# The POV label is a precedence rule over the pronouns the narration contains -- "I tell you" is first, "He tells you" is
# second, "He tells her" is third -- not a prose style. So `second` means the speaking character is narrated in third person
# while the reader is still addressed as "you", which is the commonest RP register. Asking a copy editor for "second person"
# instead gets the other reading: it makes the narration's subject "you" and turns the character's own actions into the
# reader's. Each phrase names what happens to BOTH parties for that reason, and none of them may invite a name the passage does
# not contain (see the system rules).
_POV_PHRASE = {
    "first": 'first person (the speaking character narrates their own actions as "I"), with anyone they address staying "you"',
    "second": "third person for the speaking character (their own actions become "
    '"he", "she" or "they", never "you"), while the person they address stays "you"',
    "third": 'third person throughout (no "you" anywhere in the narration)',
}
_TENSE_PHRASE = {"past": "past tense", "present": "present tense"}
# First person says nothing about the other party: "I guide you inside" and "I guide him inside" are both first person, and only
# the first still addresses the user. The addressee catches that switch. It is compared only between two first-person readings,
# so each phrase keeps the narrator as "I" and says what the person they are with becomes.
_ADDRESSEE_PHRASE = {
    "you": 'the speaking character keeps narrating as "I", and the person they are with and talking to is "you" in the '
    'narration, not "he", "she" or "they"',
    "other": 'the speaking character keeps narrating as "I", and the person they are with and talking to is "he", "she" or '
    '"they" in the narration, never "you"',
}

# The labels' precedence order. A message's POV is its highest-ranked pronoun anywhere in the narration, and so is its addressee.
_POV_RANK = {"third": 1, "second": 2, "first": 3}
_ADDRESSEE_RANK = {"other": 1, "you": 2}


def _content_digest(text: str) -> str:
    """Identify the message and the narration-extraction and reading policy that labeled it."""
    return hashlib.sha256(b"whole-narration-v5\0" + text.encode()).hexdigest()


def combine(windows: Sequence[VoiceLabels]) -> VoiceLabels:
    """Combine window labels into message POV/tense/addressee.

    POV uses precedence, tense a stable majority. First person also requires
    a third of confident windows to avoid unmarked inner-monologue false positives.
    The addressee is precedence over every window, whatever its POV: one "you"
    anywhere in the narration makes the message address "you".
    """
    povs = [pov for pov, _, _ in windows if pov in _POV_RANK]
    if 3 * povs.count("first") < len(povs):
        povs = [pov for pov in povs if pov != "first"]
    pov = max(povs, key=_POV_RANK.__getitem__) if povs else UNKNOWN
    addressees = [a for _, _, a in windows if a in _ADDRESSEE_RANK]
    addressee = max(addressees, key=_ADDRESSEE_RANK.__getitem__) if addressees else NO_ADDRESSEE
    return pov, stable_label([tense for _, tense, _ in windows], UNKNOWN), addressee


async def read(text: str, style: AxisStyle) -> VoiceLabels | None:
    """Return the (POV, tense, addressee) of *text*'s whole narration, extracted under its markup reading *style*, or ``None`` on a
    local-ML fault.

    The model reads a few sentences at a time, so every window is classified and combined. Drafts and baseline rows both go
    through here: a rewrite costs far more than reading every window, and two ends read differently disagree about messages that
    do not differ.
    """
    try:
        windows = await classify_pov_tense_chunks(narration_only(text, style.dialogue))
    except Exception as e:
        logger.warning("[format_consistency] POV/tense classification failed (%r); skipping the voice check", e)
        return None
    return combine(windows)


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
        return await read(text, style)
    digest = _content_digest(text)
    classifier = local_model_identity(FEATURE)
    cached = await get_workflow_message_state(mid, WORKFLOW_ID)
    if isinstance(cached, Mapping):
        pov, tense, addressee = cached.get("pov"), cached.get("tense"), cached.get("addressee")
        if (
            cached.get("content_sha256") == digest
            and cached.get("classifier") == classifier
            and cached.get("dialogue") == style.dialogue.value
            and isinstance(pov, str)
            and isinstance(tense, str)
            and isinstance(addressee, str)
        ):
            return pov, tense, addressee
    labels = await read(text, style)
    if labels is None:
        return None
    pov, tense, addressee = labels
    payload = dict(cached) if isinstance(cached, Mapping) else {}
    payload.update(
        {
            "pov": pov,
            "tense": tense,
            "addressee": addressee,
            "dialogue": style.dialogue.value,
            "content_sha256": digest,
            "classifier": classifier,
        }
    )
    await set_workflow_message_state(mid, WORKFLOW_ID, payload)
    return labels


def target(window_labels: list[VoiceLabels]) -> VoiceLabels:
    """Return independently voted target (POV, tense, addressee) labels."""
    return (
        stable_label([pov for pov, _, _ in window_labels], UNKNOWN),
        stable_label([tense for _, tense, _ in window_labels], UNKNOWN),
        stable_label([addressee for _, _, addressee in window_labels], NO_ADDRESSEE),
    )


def drift(source: VoiceLabels, target_labels: VoiceLabels) -> list[str]:
    """Return human phrases for confident voice axes that drifted.

    The addressee is compared only when both sides read first person: a POV phrase already names both parties, and outside
    first person the addressee follows from the POV.
    """
    phrases: list[str] = []
    for src, tgt, table in ((source[0], target_labels[0], _POV_PHRASE), (source[1], target_labels[1], _TENSE_PHRASE)):
        if src != UNKNOWN and tgt != UNKNOWN and src != tgt and tgt in table:
            phrases.append(table[tgt])
    src, tgt = source[2], target_labels[2]
    if source[0] == target_labels[0] == "first" and NO_ADDRESSEE not in (src, tgt) and src != tgt and tgt in _ADDRESSEE_PHRASE:
        phrases.append(_ADDRESSEE_PHRASE[tgt])
    return phrases
