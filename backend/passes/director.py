"""
passes/director.py — Director pass: the tool-calling phase that selects
moods, plot direction, and optionally rewrites the user's prompt before
the writer pass runs.
"""

from __future__ import annotations

import json
import logging
import time
from typing import AsyncIterator, List, Optional

from ..llm_client import LLMClient, parse_tool_calls, reasoning_cfg
from ..tool_defs import TOOLS, POST_WRITER_TOOLS, enabled_schemas, build_direct_scene_tool
from ..prompt_builder import build_tool_prompt

logger = logging.getLogger(__name__)


# ── Tool-call result unpacking ────────────────────────────────────────────────


def apply_tool_calls(
    tool_calls: list[dict],
    current_moods: list[str],
) -> tuple[list[str], str | None, dict]:
    """Extract values from tool calls.

    Returns (moods, refined_message, extra_fields).
    extra_fields contains all direct_scene args except moods.
    """
    moods = list(current_moods)
    refined: str | None = None
    extra_fields: dict = {}

    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "direct_scene":
            moods = args.get("moods", [])
            extra_fields = {
                k: v
                for k, v in args.items()
                if k != "moods" and v not in (None, "", [])
            }
        elif tc["name"] == "rewrite_user_prompt":
            refined = args.get("refined_message") or None

    return (moods, refined, extra_fields)


# ── Agent pass ────────────────────────────────────────────────────────────────


async def _director_pass(
    client: LLMClient,
    prefix: list[dict],
    user_message: str,
    settings: dict,
    director: dict,
    fragments: list[dict],
    director_fragments: list[dict],
    enabled_tools: dict | None = None,
    attachments: Optional[List[dict]] = None,
    kv_tracker=None,
    reasoning_on: bool = True,
) -> AsyncIterator[dict]:
    """Yields reasoning dicts during each tool call, then a single done dict.

    Yields:
        {"type": "reasoning", "delta": str}   — zero or more reasoning chunks
        {"type": "done", "result": tuple}     — final (moods, raw, calls, latency, refined, extra_fields)
    """
    active_moods = director["active_moods"]
    if attachments is None:
        attachments = []

    refined_msg: str | None = None
    extra_fields: dict = {}
    all_calls: list[dict] = []
    last_raw = ""

    tool_names = (
        ["direct_scene"]
        if enabled_tools is None
        else [
            n
            for n, on in enabled_tools.items()
            if on and n in TOOLS and n not in POST_WRITER_TOOLS
        ]
    )

    # Enforce priority order: rewrite_user_prompt first so users can abort
    # early if they dislike the rewrite before the full director runs.
    if len(tool_names) > 1:
        priority = ["rewrite_user_prompt", "direct_scene"]
        tool_names.sort(
            key=lambda x: priority.index(x) if x in priority else len(priority)
        )

    if not tool_names:
        yield {
            "type": "done",
            "result": (active_moods, "", [], 0, None, {}),
        }
        return

    # Build base schemas, replacing the static direct_scene with the dynamic one.
    base_schemas = enabled_schemas(enabled_tools)
    dynamic_direct_scene = build_direct_scene_tool(director_fragments)
    tool_schemas = [
        dynamic_direct_scene if s["function"]["name"] == "direct_scene" else s
        for s in base_schemas
    ]

    logger.info(
        "Director pass: tools included=%s",
        (
            json.dumps([s["function"]["name"] for s in tool_schemas])
            if tool_schemas
            else "[]"
        ),
    )

    t0 = time.monotonic()
    for name in tool_names:
        tail = build_tool_prompt(name, user_message, active_moods, fragments)
        if attachments:
            parts = [{"type": "text", "text": tail}]
            for att in attachments:
                mime = att.get("mime_type", att.get("mime", "image/jpeg"))
                b64 = att.get("data_b64", att.get("b64", ""))
                if not b64:
                    continue
                url = f"data:{mime};base64,{b64}"
                parts.append({"type": "image_url", "image_url": {"url": url}})
            content = parts
        else:
            content = tail
        msgs = prefix + [{"role": "user", "content": content}]
        logger.info(
            "Agent tool=%s prompt:\n%s",
            name,
            json.dumps(msgs, indent=2, ensure_ascii=False),
        )
        if kv_tracker is not None:
            kv_tracker.record(f"director:{name}", msgs, tool_schemas)
        resp: dict = {}
        try:
            reasoning_params = (
                reasoning_cfg(False)
                if not reasoning_on or name == "rewrite_user_prompt"
                else reasoning_cfg(True)
            )
            async for event in client.complete(
                messages=msgs,
                model=settings["model_name"],
                tools=tool_schemas,
                tool_choice=TOOLS[name]["choice"],
                temperature=0.25,
                max_tokens=8192,
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
                active_moods, new_refined, new_extra = apply_tool_calls(parsed, active_moods)
                if new_refined:
                    refined_msg = new_refined
                if new_extra:
                    extra_fields.update(new_extra)
            else:
                logger.info("Agent tool=%s: model skipped", name)
        except Exception as e:
            logger.error("Agent tool=%s failed: %s", name, e)
            last_raw = f"ERROR: {e}"

    yield {
        "type": "done",
        "result": (
            active_moods,
            last_raw,
            all_calls,
            int((time.monotonic() - t0) * 1000),
            refined_msg,
            extra_fields,
        ),
    }
