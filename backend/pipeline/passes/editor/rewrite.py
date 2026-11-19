"""Read the whole new draft an ``editor_rewrite`` call carries."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from ....core.llm_types import CompletionMessage, ParsedToolCall

logger = logging.getLogger(__name__)


def rewritten_text(calls: Sequence[ParsedToolCall], resp: CompletionMessage, *, label: str) -> str:
    """The first ``editor_rewrite`` call's stripped ``rewritten_text``, or ``""`` when it cannot replace the draft.

    A missing call or a missing, null, non-string or blank text is the model declining the forced call. A reply the token
    limit cut off is refused too: its rewrite may end mid-reply, and it would replace the whole draft. Each refusal is logged
    under *label*.
    """
    call = next((call for call in calls if call.get("name") == "editor_rewrite"), None)
    if call is None:
        logger.warning("%s: no editor_rewrite call, draft kept", label)
        return ""
    if (resp.get("finish_reason") or resp.get("stop_reason")) == "length":
        logger.warning("%s: the rewrite hit the token limit, draft kept", label)
        return ""
    raw = call.get("arguments", {}).get("rewritten_text")
    rewritten = raw.strip() if isinstance(raw, str) else ""
    if not rewritten:
        logger.warning("%s: empty rewritten_text, draft kept: %r", label, raw)
    return rewritten
