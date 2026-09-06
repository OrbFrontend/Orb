"""Select effective lorebook entries for the current scene."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field

from ....core import extract_hyperparams
from ....inference import (
    CachedBase,
    LLMClient,
    parse_tool_calls,
    reasoning_cfg,
)
from ....prompting.lorebook import director_pick_diagnostics
from ....prompting.tool_schemas import SELECT_LOREBOOK_CHOICE
from .prompts import build_lorebook_select_prompt

logger = logging.getLogger(__name__)


def _log_director_pick_diagnostics(
    entries: Sequence[Mapping[str, object]],
    picks: Sequence[str],
) -> None:
    recovered, unmatched = director_pick_diagnostics(entries, picks)
    if recovered:
        logger.warning(
            "Lorebook: %d director pick(s) matched only after stripping catalog delimiters: %s",
            len(recovered),
            ", ".join(repr(pick) for pick in recovered),
        )
    if unmatched:
        logger.info(
            "Lorebook: %d director pick(s) named no entry: %s",
            len(unmatched),
            ", ".join(repr(pick) for pick in unmatched),
        )


@dataclass(slots=True)
class LorebookSelectResult:
    """Typed result of the lorebook-select step, yielded as the ``done`` payload.

    ``selected`` is the list of chosen entry names (fed into the writer's lorebook
    block); ``calls`` is the parsed ``select_lorebook`` call, appended to the turn's
    tool calls so the picks stay visible in the conversation log / inspector.
    """

    selected: list[str] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)


async def lorebook_select_step(
    client: LLMClient,
    base: CachedBase,
    *,
    settings: Mapping[str, object],
    catalog: str,
    user_message: str,
    entries: Sequence[Mapping[str, object]] | None = None,
    kv_tracker=None,
    reasoning_on: bool = False,
    reasoning_prefill: str = "",
) -> AsyncIterator[dict]:
    """Yield reasoning chunks during the call, then a single done dict.

    One forced ``select_lorebook`` call; the catalog rides the OOC trailing.

    Yields:
        ``{"type": "reasoning", "delta": str}``
        ``{"type": "done", "result": LorebookSelectResult}``
    """
    if not catalog:
        yield {"type": "done", "result": LorebookSelectResult()}
        return

    request = build_lorebook_select_prompt(catalog, user_message, reasoning_on=reasoning_on)
    trailing = [{"role": "user", "content": request}]
    hyperparams = extract_hyperparams(settings, lane="agent", token_floor=2048, defaults={"temperature": 0.25})

    resp: dict = {}
    try:
        async for event in base.complete_into(
            client,
            resp,
            label="select_lorebook",
            trailing=trailing,
            tool_choice=SELECT_LOREBOOK_CHOICE,
            kv_tracker=kv_tracker,
            **hyperparams,
            **reasoning_cfg(reasoning_on, reasoning_prefill),
        ):
            yield event
    except Exception:
        # A failed call selects nothing but must not propagate: the writer still runs
        # with the deterministic constant/keyword lorebook entries.
        logger.exception("Lorebook-select call failed; selecting nothing")
        yield {"type": "done", "result": LorebookSelectResult()}
        return

    logger.info("Lorebook-select step output:\n%s", json.dumps(resp, default=str))
    calls = parse_tool_calls(resp)
    selected: list[str] = []
    for tc in calls:
        if tc.get("name") == "select_lorebook":
            picks = tc.get("arguments", {}).get("selected_lorebook_entries")
            if isinstance(picks, list):
                selected = [str(x) for x in picks]

    if entries is not None:
        _log_director_pick_diagnostics(entries, selected)

    yield {"type": "done", "result": LorebookSelectResult(selected=selected, calls=calls)}
