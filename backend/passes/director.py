"""
passes/director.py — Director pass: the pre-processing phase that selects
moods, plot direction, and optionally rewrites the user's prompt before
the writer pass runs.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Mapping, Optional, Sequence

from ..llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ..kv_tracker import CachedBase
from ..tool_defs import (
    TOOLS,
    PRE_WRITER_TOOLS,
)
from ..prompt_builder import build_director_tool_prompt
from ..llm_types import ChatMessage
from ..utils import extract_hyperparams, build_multimodal_content

logger = logging.getLogger(__name__)


@dataclass
class DirectorResult:
    """Typed payload of the director pass's terminal ``done`` event.

    Field names match the orchestrator's turn-state locals and
    :class:`~backend.orchestrator._PipelineResult` (notably ``agent_raw``,
    ``rewritten_msg``) so a single name follows each value end to end. This
    replaces the former 6-positional ``result`` tuple: adding or reordering a
    field can no longer silently transpose values at the unpack site.

    ``progressive_fields`` is intentionally absent — it is derived in the
    orchestrator from ``extra_fields`` filtered against the valid progressive
    fragment ids, which the director pass does not know about.
    """

    active_moods: list[str] = field(default_factory=list)
    agent_raw: str = ""
    calls: list[dict] = field(default_factory=list)
    latency: int = 0
    rewritten_msg: str | None = None
    extra_fields: dict = field(default_factory=dict)
    selected_lorebook_entries: list[str] = field(default_factory=list)


# ── Tool-call result unpacking ────────────────────────────────────────────────


def apply_tool_calls(
    tool_calls: list[dict],
    current_moods: list[str],
) -> tuple[list[str], str | None, dict, list[str]]:
    """Extract values from tool calls.

    Returns (moods, refined_message, extra_fields, selected_lorebook_entries).
    extra_fields contains all direct_scene args except moods and selected_lorebook_entries.
    selected_lorebook_entries is the Director's agentic lorebook selection (entry names);
    it is pulled out explicitly so it never renders as a Scene Direction field.
    """
    moods = list(current_moods)
    refined: str | None = None
    extra_fields: dict = {}
    selected_lorebook_entries: list[str] = []

    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "direct_scene":
            moods = args.get("moods", [])
            al = args.get("selected_lorebook_entries")
            selected_lorebook_entries = [str(x) for x in al] if isinstance(al, list) else []
            extra_fields = {
                k: v for k, v in args.items() if k not in ("moods", "selected_lorebook_entries") and v not in (None, "", [])
            }
        elif tc["name"] == "rewrite_user_prompt":
            refined = args.get("refined_message") or None

    return (moods, refined, extra_fields, selected_lorebook_entries)


# ── Agent pass ────────────────────────────────────────────────────────────────


async def director_pass(
    client: LLMClient,
    base: CachedBase,
    user_message: str,
    settings: Mapping[str, Any],
    director: Mapping[str, Any],
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    enabled_tools: Mapping[str, bool],
    attachments: Optional[Sequence[Mapping[str, Any]]] = None,
    kv_tracker=None,
    reasoning_on: bool = True,
    lorebook_block: str = "",
    lorebook_catalog: str = "",
    progressive_state: dict | None = None,
) -> AsyncIterator[dict]:
    """Yields reasoning dicts during each tool call, then a single done dict.

    Yields:
        {"type": "reasoning", "delta": str}        — zero or more reasoning chunks
        {"type": "done", "result": DirectorResult}  — terminal pass result
    """
    active_moods = director["active_moods"]
    if attachments is None:
        attachments = []

    refined_msg: str | None = None
    extra_fields: dict = {}
    selected_lorebook_entries: list[str] = []
    all_calls: list[dict] = []
    last_raw = ""

    tool_names = [n for n, on in enabled_tools.items() if on and n in PRE_WRITER_TOOLS]

    # Enforce priority order: rewrite_user_prompt first so users can abort
    # early if they dislike the rewrite before the full director runs.
    if len(tool_names) > 1:
        priority = ["rewrite_user_prompt", "direct_scene"]
        tool_names.sort(key=lambda x: priority.index(x) if x in priority else len(priority))

    if not tool_names:
        yield {
            "type": "done",
            "result": DirectorResult(active_moods=active_moods),
        }
        return

    # The tools blob is resolved once into the shared base; the director reads it
    # rather than rebuilding it, so it cannot drift from the writer/editor blobs.
    tool_schemas = list(base.tools)

    logger.info(
        "Director pass: tools included=%s",
        (json.dumps([s["function"]["name"] for s in tool_schemas]) if tool_schemas else "[]"),
    )

    t0 = time.monotonic()
    for name in tool_names:
        if client.is_aborted:
            break
        tool_schema = next((s for s in tool_schemas if s["function"]["name"] == name), None)
        tool_tail = build_director_tool_prompt(
            name,
            user_message,
            active_moods,
            mood_fragments,
            reasoning_on=reasoning_on,
            interactive_fragments=interactive_fragments,
            progressive_state=progressive_state,
            tool_schema=tool_schema,
            lorebook_catalog=lorebook_catalog,
        )
        tail = ("___\n\n" + lorebook_block + "\n\n" if lorebook_block else "") + tool_tail
        content = build_multimodal_content(tail, attachments)
        trailing: list[ChatMessage] = [{"role": "user", "content": content}]
        logger.info(
            "Agent tool=%s prompt:\n%s",
            name,
            json.dumps([*base.prefix, *trailing], indent=2, ensure_ascii=False),
        )
        resp: dict = {}
        # Errors are not caught here: a failed tool call propagates out of the
        # pass and aborts the turn, like the writer/editor passes.
        reasoning_params = reasoning_cfg(reasoning_on and name != "rewrite_user_prompt")
        hyperparams = extract_hyperparams(settings, defaults={"temperature": 0.25, "max_tokens": 8192})
        async for event in base.complete(
            client,
            label=f"director:{name}",
            trailing=trailing,
            tool_choice=TOOLS[name]["choice"],
            kv_tracker=kv_tracker,
            **hyperparams,
            **reasoning_params,
        ):
            if event["type"] == "reasoning":
                yield {"type": "reasoning", "delta": event["delta"]}
            elif event["type"] == "done":
                resp = event["message"]
        last_raw = json.dumps(resp, default=str)
        logger.info("Agent tool=%s output:\n%s", name, last_raw)
        if parsed := parse_tool_calls(resp):
            all_calls.extend(parsed)
            active_moods, new_refined, new_extra, new_lorebook = apply_tool_calls(parsed, active_moods)
            if new_refined:
                refined_msg = new_refined
            if new_extra:
                extra_fields.update(new_extra)
            if new_lorebook:
                selected_lorebook_entries = new_lorebook
        else:
            logger.info("Agent tool=%s: model skipped", name)

    yield {
        "type": "done",
        "result": DirectorResult(
            active_moods=active_moods,
            agent_raw=last_raw,
            calls=all_calls,
            latency=int((time.monotonic() - t0) * 1000),
            rewritten_msg=refined_msg,
            extra_fields=extra_fields,
            selected_lorebook_entries=selected_lorebook_entries,
        ),
    }
