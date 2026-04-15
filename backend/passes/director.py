"""
passes/director.py — Director pass: the tool-calling phase that selects
moods, plot direction, and optionally rewrites the user's prompt before
the writer pass runs.
"""
from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator

from ..llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ..tool_defs import TOOLS, POST_WRITER_TOOLS, enabled_schemas
from ..prompt_builder import build_tool_prompt

logger = logging.getLogger(__name__)


# ── Tool-call result unpacking ────────────────────────────────────────────────

def apply_tool_calls(
    tool_calls: list[dict], current_moods: list[str],
) -> tuple[list[str], str | None, str | None, str | None, list[str] | None, str | None, list[str] | None]:
    moods = list(current_moods)
    refined, next_action, writing_direction, detected_repetitions, plot_summary, keywords = (
        None, None, None, None, None, None,
    )
    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "direct_scene":
            moods                = args.get("moods", [])
            next_action          = args.get("next_action") or None
            writing_direction    = args.get("writing_direction") or None
            detected_repetitions = args.get("detected_repetitions") or None
            plot_summary         = args.get("plot_summary") or None
            keywords             = args.get("keywords") or None
        elif tc["name"] == "rewrite_user_prompt":
            refined = args.get("refined_message") or None
    return moods, refined, next_action, writing_direction, detected_repetitions, plot_summary, keywords


# ── Agent pass ────────────────────────────────────────────────────────────────

async def _agent_pass(
    client: LLMClient, prefix: list[dict], user_message: str, settings: dict,
    director: dict, fragments: list[dict], enabled_tools: dict | None = None,
    kv_tracker=None, reasoning_on: bool = True,
) -> AsyncIterator[dict]:
    """Yields reasoning dicts during each tool call, then a single done dict.

    Yields:
        {"type": "reasoning", "delta": str}   — zero or more reasoning chunks
        {"type": "done", "result": tuple}     — final (moods, raw, calls, latency, ...)
    """
    active_moods = director["active_moods"]
    refined_msg, next_action, writing_direction, detected_repetitions, plot_summary = (
        None, None, None, None, None,
    )
    keywords = director.get("keywords", [])
    all_calls: list[dict] = []
    last_raw = ""

    tool_names = ["direct_scene"] if enabled_tools is None else [
        n for n, on in enabled_tools.items() if on and n in TOOLS and n not in POST_WRITER_TOOLS
    ]

    # Enforce priority order: rewrite_user_prompt first so users can abort
    # early if they dislike the rewrite before the full director runs.
    if len(tool_names) > 1:
        priority = ["rewrite_user_prompt", "direct_scene"]
        tool_names.sort(key=lambda x: priority.index(x) if x in priority else len(priority))

    if not tool_names:
        yield {"type": "done", "result": (active_moods, "", [], 0, None, None, None, None, None, None)}
        return

    tool_schemas = enabled_schemas(enabled_tools)
    logger.info(
        "Director pass: tools included=%s",
        json.dumps([s["function"]["name"] for s in tool_schemas]) if tool_schemas else "[]",
    )

    t0 = time.monotonic()
    for name in tool_names:
        msgs = prefix + [{"role": "user", "content": build_tool_prompt(name, user_message, active_moods, fragments)}]
        logger.info("Agent tool=%s prompt:\n%s", name, json.dumps(msgs, indent=2, ensure_ascii=False))
        if kv_tracker is not None:
            kv_tracker.record(f"director:{name}", msgs, tool_schemas)
        resp: dict = {}
        try:
            reasoning_params = reasoning_cfg(False) if not reasoning_on else reasoning_cfg(True)
            async for event in client.complete(
                messages=msgs, model=settings["model_name"], tools=tool_schemas,
                tool_choice=TOOLS[name]["choice"], temperature=0.25, max_tokens=8192,
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
                active_moods, new_refined, new_plot, new_narration, new_reps, new_summary, new_kw = apply_tool_calls(parsed, active_moods)
                if new_refined:    refined_msg           = new_refined
                if new_plot:       next_action        = new_plot
                if new_narration:  writing_direction     = new_narration
                if new_reps:       detected_repetitions  = new_reps
                if new_summary:    plot_summary          = new_summary
                if new_kw:         keywords              = new_kw[:6]
            else:
                logger.info("Agent tool=%s: model skipped", name)
        except Exception as e:
            logger.error("Agent tool=%s failed: %s", name, e)
            last_raw = f"ERROR: {e}"

    yield {
        "type": "done",
        "result": (
            active_moods, last_raw, all_calls,
            int((time.monotonic() - t0) * 1000),
            refined_msg, next_action, writing_direction,
            detected_repetitions, plot_summary, keywords,
        ),
    }
