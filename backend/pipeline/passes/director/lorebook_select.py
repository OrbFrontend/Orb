"""
passes/director/lorebook_select.py -- Agentic-lorebook selection step.

Asks the model, via a forced ``select_lorebook`` call, which lorebook entries from the
catalog belong in the current scene. The step is deliberately independent of the
``direct_scene`` tool, so agentic lorebook works whether or not the Director's
scene-direction tool is enabled
(gated only by ``agentic_lorebook_enabled`` + the global agent + a non-constant entry).

The wire schema is the fixed ``select_lorebook`` tool held in the shared per-turn blob, so
the call reuses the cached base and only forces the tool choice; the selectable catalog
rides this step's OOC trailing. Errors and aborts are swallowed into an empty selection --
a bad pick must never crash the turn.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator, Mapping

from ....core import extract_hyperparams
from ....inference import (
    SELECT_LOREBOOK_CHOICE,
    CachedBase,
    LLMClient,
    build_lorebook_select_prompt,
    parse_tool_calls,
    reasoning_cfg,
)

logger = logging.getLogger(__name__)


@dataclass
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
    kv_tracker=None,
    reasoning_on: bool = False,
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
    hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 2048})

    resp: dict = {}
    try:
        async for event in base.complete(
            client,
            label="select_lorebook",
            trailing=trailing,
            tool_choice=SELECT_LOREBOOK_CHOICE,
            kv_tracker=kv_tracker,
            **hyperparams,
            **reasoning_cfg(reasoning_on),
        ):
            if event["type"] == "reasoning":
                yield {"type": "reasoning", "delta": event["delta"]}
            elif event["type"] == "done":
                resp = event["message"]
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

    yield {"type": "done", "result": LorebookSelectResult(selected=selected, calls=calls)}
