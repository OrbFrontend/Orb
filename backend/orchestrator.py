"""
orchestrator.py — Main pipeline: agent pass → writer pass → refine pass,
plus the public entry points handle_turn() and handle_regenerate().
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import AsyncIterator

from . import database as db
from .llm_client import LLMClient, parse_tool_calls
from .tool_defs import (
    TOOLS, POST_WRITER_TOOLS, enabled_schemas,
    reasoning_config_for_tool,
)
from .prompt_builder import build_prefix, build_tool_prompt, build_style_injection
from .refine import refine_pass

logger = logging.getLogger(__name__)


# ── Tool-call result unpacking

def apply_tool_calls(tool_calls: list[dict], current_moods: list[str]) -> tuple[list[str], str | None, str | None, str | None, list[str] | None, str | None, list[str] | None]:
    moods, refined, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords = list(current_moods), None, None, None, None, None, None
    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "direct_scene":
            moods = args.get("moods", [])
            plot_direction = args.get("plot_direction") or None
            writing_direction = args.get("writing_direction") or None
            detected_repetitions = args.get("detected_repetitions") or None
            plot_summary = args.get("plot_summary") or None
            keywords = args.get("keywords") or None
        elif tc["name"] == "rewrite_user_prompt":
            refined = args.get("refined_message") or None
    return moods, refined, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords


# ── Character context loader

async def _load_char_context(conv: dict, settings: dict) -> tuple[str, str, str]:
    system_prompt = settings["system_prompt"]
    char_persona, mes_example = "", ""
    if card_id := conv.get("character_card_id"):
        card = await db.get_character_card(card_id)
        if card:
            char_persona = "\n\n".join(filter(None, [card.get("description", ""), card.get("personality", "")]))
            mes_example = card.get("mes_example", "")
            if card.get("system_prompt"):
                system_prompt = card["system_prompt"]
    return system_prompt, char_persona, mes_example


# ── Writer pass

# Token strings that signal the start of a tool-call block.  If logit_bias
# suppression fails (wrong ID, unsupported by backend, etc.) and one of these
# leaks into the writer's output stream, we truncate immediately so the user
# never sees the raw markup.
_WRITER_LEAK_MARKERS = {
    "<|tool_call>",
    "<|python_tag|>",
    "[TOOL_CALL]",
    "<tool_call>",
    "<function_calls>",
    "<|tool_calls|>",
    "<|function_calls|>",
}


async def _writer_pass(client: LLMClient, msgs: list[dict], settings: dict, enabled_tools: dict | None = None, tool_start_token_id: int | None = None) -> AsyncIterator[str]:
    params = {k: v for k in ["temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty"] if (v := settings.get(k)) is not None}
    schemas = enabled_schemas(enabled_tools)
    # Only include tool schemas when we have a confirmed suppression token.
    # Without logit_bias, small models ignore tool_choice:"none" and emit tool-call tokens anyway, causing hallucinated output.
    if schemas and tool_start_token_id is None:
        logger.info("Writer pass: skipping tools (no suppression token discovered) to prevent hallucination")
        schemas = []
    logger.info("Writer pass: tools included=%s", json.dumps([s["function"]["name"] for s in schemas]) if schemas else "[]")
    extra = {"tools": schemas, "tool_choice": "none"} if schemas else {}
    if tool_start_token_id is not None:
        extra["logit_bias"] = {tool_start_token_id: -100}
        logger.info("Writer pass: logit_bias {%d: -100} applied", tool_start_token_id)

    # Rolling tail buffer: most control tokens arrive as a single delta, but we keep the last 50 chars to catch any that straddle a token boundary.
    tail = ""
    async for token in client.stream(messages=msgs, model=settings["model_name"], **extra, **params):
        tail = (tail + token)[-50:]
        for marker in _WRITER_LEAK_MARKERS:
            if marker in tail:
                logger.warning(
                    "Writer pass: tool-call marker '%s' leaked through suppression — truncating output",
                    marker,
                )
                return
        yield token


# ── Agent pass

async def _agent_pass(
    client: LLMClient, prefix: list[dict], user_message: str, settings: dict,
    director: dict, fragments: list[dict], enabled_tools: dict | None = None
) -> tuple[list[str], str, list, int, str | None, str | None, str | None, list[str] | None, str | None, list[str] | None]:
    active_moods = director["active_moods"]
    refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary = None, None, None, None, None
    keywords = director.get("keywords", [])
    all_calls: list[dict] = []
    last_raw = ""

    tool_names = ["direct_scene"] if enabled_tools is None else [
        n for n, on in enabled_tools.items() if on and n in TOOLS and n not in POST_WRITER_TOOLS
    ]

    # Define priority order: rewrite_user_prompt first, then direct_scene
    if len(tool_names) > 1:
        priority_order = ["rewrite_user_prompt", "direct_scene"]
        tool_names.sort(key=lambda x: priority_order.index(x) if x in priority_order else len(priority_order))
    if not tool_names:
        return active_moods, "", [], 0, None, None, None, None, None, None

    tool_schemas = enabled_schemas(enabled_tools)
    logger.info("Director pass: tools included=%s", json.dumps([s["function"]["name"] for s in tool_schemas]) if tool_schemas else "[]")

    t0 = time.monotonic()
    for name in tool_names:
        msgs = prefix + [{"role": "user", "content": build_tool_prompt(name, user_message, active_moods, fragments)}]
        logger.info("Agent tool=%s prompt:\n%s", name, json.dumps(msgs, indent=2, ensure_ascii=False))
        try:
            reasoning_config = reasoning_config_for_tool(name)
            resp = await client.complete(
                messages=msgs, model=settings["model_name"], tools=tool_schemas,
                tool_choice=TOOLS[name]["choice"], temperature=0.25, max_tokens=8192,
                **({"reasoning": reasoning_config} if reasoning_config else {}),
            )
            last_raw = json.dumps(resp, default=str)
            logger.info("Agent tool=%s output:\n%s", name, last_raw)
            if parsed := parse_tool_calls(resp):
                all_calls.extend(parsed)
                active_moods, new_refined, new_plot, new_narration, new_reps, new_summary, new_kw = apply_tool_calls(parsed, active_moods)
                if new_refined:
                    refined_msg = new_refined
                if new_plot:
                    plot_direction = new_plot
                if new_narration:
                    writing_direction = new_narration
                if new_reps:
                    detected_repetitions = new_reps
                if new_summary:
                    plot_summary = new_summary
                if new_kw:
                    keywords = new_kw[:6]
            else:
                logger.info("Agent tool=%s: model skipped", name)
        except Exception as e:
            logger.error("Agent tool=%s failed: %s", name, e)
            last_raw = f"ERROR: {e}"

    return active_moods, last_raw, all_calls, int((time.monotonic() - t0) * 1000), refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords


# ── Core pipeline

async def _run_pipeline(
    client: LLMClient, settings: dict, director: dict, fragments: list[dict],
    prefix: list[dict], user_message: str, phrase_bank: list[list[str]] | None = None,
) -> AsyncIterator[dict]:
    """Three-pass pipeline: director → writer → refine.

    KV cache strategy: *prefix* (system prompt + chat history) and the tool
    schema list returned by ``enabled_schemas(enabled_tools)`` are kept
    identical across all three passes so the LLM can reuse cached KV entries.
    Only ``tool_choice`` and the trailing user message differ per pass.
    The refine pass may append REFINE_REWRITE_TOOL when the length guard
    fires — an intentional cache-miss that rarely occurs.
    """
    enabled_tools = settings.get("enabled_tools") or {}
    agent_on = bool(settings.get("enable_agent", 1))
    if not agent_on:
        enabled_tools = {}

    active_moods = director["active_moods"]
    agent_raw, calls, latency = "", [], 0
    refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary = None, None, None, None, None
    keywords = director.get("keywords", [])
    effective_msg = user_message

    audit_enabled = agent_on and bool(enabled_tools.get("refine_apply_patch", False)) and phrase_bank is not None

    # Length guard
    length_guard_enabled = bool(enabled_tools.get("length_guard", False)) if agent_on else False
    length_guard_enforce = bool(enabled_tools.get("length_guard_enforce", False)) if agent_on else False
    length_guard = {
        "enabled": length_guard_enabled,
        "max_words": int(settings.get("length_guard_max_words", 240)),
        "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
    } if (length_guard_enabled and agent_on) else None

    do_refine = audit_enabled or (length_guard_enabled and agent_on)

    # --- Agent pass ---
    has_pre_writer_tools = any(enabled_tools.get(n, False) for n in TOOLS if n not in POST_WRITER_TOOLS)
    if agent_on and has_pre_writer_tools:
        yield {"event": "director_start"}
        active_moods, agent_raw, calls, latency, refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords = await _agent_pass(
            client, prefix, user_message, settings, director, fragments, enabled_tools
        )
        if refined_msg:
            effective_msg = refined_msg
            yield {"event": "prompt_rewritten", "data": {"refined_message": refined_msg}}

    # Style injection
    # Only use stored moods/keywords when direct_scene is enabled; otherwise
    # the previous turn's director state would bleed into <current_scene_direction>
    # even though the director tool has been disabled.
    direct_scene_enabled = agent_on and bool(enabled_tools.get("direct_scene", False))
    if direct_scene_enabled:
        inj_active_moods = active_moods
        inj_keywords = keywords
    else:
        inj_active_moods = []
        inj_keywords = []
    deactivated = [f for f in fragments if f["id"] in (set(director["active_moods"]) - set(inj_active_moods))] if direct_scene_enabled else []
    active = [f for f in fragments if f["id"] in inj_active_moods]
    inj_block = build_style_injection(active, deactivated, plot_direction, writing_direction, detected_repetitions, plot_summary, inj_keywords) if (active or deactivated or plot_direction or writing_direction or detected_repetitions or plot_summary or inj_keywords) else ""

    yield {"event": "director_done", "data": {
        "active_moods": active_moods, "injection_block": inj_block, "tool_calls": calls,
        "agent_latency_ms": latency, "plot_direction": plot_direction, "writing_direction": writing_direction,
        "detected_repetitions": detected_repetitions, "plot_summary": plot_summary, "keywords": keywords,
    }}

    # --- Resolve tool-start token for writer logit bias ---
    # Only needed when tool schemas are sent during the writer pass (for KV cache).
    tool_start_token_id: int | None = None
    if enabled_schemas(enabled_tools):
        model_key = f"{settings['endpoint_url']}||{settings['model_name']}"
        cached, cached_id = await db.get_tool_start_token(model_key)
        if cached:
            tool_start_token_id = cached_id
        else:
            tool_start_token_id = await client.discover_tool_start_token(settings["model_name"])
            await db.set_tool_start_token(model_key, tool_start_token_id)

    # --- Writer pass ---
    writer_tail = ""
    if inj_block:
        writer_tail += inj_block + "\n\n"
    writer_tail += "**Do not use tool or function calls.**\n\n"
    if length_guard_enforce and length_guard and length_guard.get("enabled"):
        max_words = length_guard.get("max_words", 240)
        max_paragraphs = length_guard.get("max_paragraphs", 4)
        writer_tail += f"[Keep your response under {max_words} words and {max_paragraphs} paragraphs.]\n\n"
    writer_tail += effective_msg + "\n\n"
    # writer_tail += "[OOC: Tool/Function calling is STRICTLY FORBIDDEN now!]\n\n" + effective_msg + "\n\n"
    # writer_tail += effective_msg + "\n\n"

    writer_msgs = prefix + [{"role": "user", "content": writer_tail}]

    resp_text = ""
    async for token in _writer_pass(client, writer_msgs, settings, enabled_tools, tool_start_token_id):
        resp_text += token
        yield {"event": "token", "data": token}

    yield {"event": "_result", "data": {
        "active_moods": active_moods, "agent_raw": agent_raw, "calls": calls,
        "latency": latency, "refined_msg": refined_msg, "effective_msg": effective_msg,
        "resp_text": resp_text, "inj_block": inj_block, "plot_direction": plot_direction,
        "writing_direction": writing_direction, "detected_repetitions": detected_repetitions,
        "plot_summary": plot_summary, "keywords": keywords,
    }}

    # --- Refine pass ---
    if do_refine and resp_text:
        logger.info("Refine pass starting (draft=%d chars, phrase_bank=%d groups)", len(resp_text), len(phrase_bank) if phrase_bank else 0)
        try:
            refined_draft, _debug_log, _elapsed = await refine_pass(client, prefix, effective_msg, resp_text, settings, phrase_bank or [], audit_enabled, length_guard, enabled_tools)
            if refined_draft and refined_draft != resp_text:
                resp_text = refined_draft
                yield {"event": "writer_rewrite", "data": {"refined_text": resp_text}}
                yield {"event": "_refined_result", "data": {"resp_text": resp_text}}
        except Exception as e:
            logger.error("refine pass failed, keeping original: %s", e, exc_info=True)
    elif not do_refine:
        logger.info("Refine pass skipped (do_refine=%s)", do_refine)


# ═══════════════════════════════════════════════════════════════════════
# Shared infrastructure for handle_turn / handle_regenerate
# ═══════════════════════════════════════════════════════════════════════

async def _load_pipeline_context(conversation_id: str) -> dict | None:
    """Load everything the pipeline needs: settings, conversation, director,
    fragments, phrase_bank, and an LLMClient.

    Returns a dict of resolved objects, or None if the conversation was not found.
    """
    settings = await db.get_settings()
    conv = await db.get_conversation(conversation_id)
    if not conv:
        return None

    director = await db.get_director_state(conversation_id)
    fragments = await db.get_fragments()
    # Filter out disabled fragments
    fragments = [f for f in fragments if f.get("enabled", True)]
    # Remove disabled fragments from active moods
    if director and director.get("active_moods"):
        enabled_ids = {f["id"] for f in fragments}
        director["active_moods"] = [mood for mood in director["active_moods"] if mood in enabled_ids]
    phrase_bank = await db.get_phrase_bank()
    client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

    system_prompt, char_persona, mes_example = await _load_char_context(conv, settings)

    return {
        "settings": settings,
        "conv": conv,
        "director": director,
        "fragments": fragments,
        "phrase_bank": phrase_bank,
        "client": client,
        "system_prompt": system_prompt,
        "char_persona": char_persona,
        "mes_example": mes_example,
    }


def _build_prefix_from_ctx(ctx: dict, history: list[dict]) -> list[dict]:
    """Build the LLM prefix from a pipeline-context dict."""
    conv = ctx["conv"]
    settings = ctx["settings"]
    return build_prefix(
        ctx["system_prompt"], conv["character_name"], ctx["char_persona"],
        conv["character_scenario"], ctx["mes_example"],
        conv.get("post_history_instructions", ""),
        history, settings.get("user_name", "User"), settings.get("user_description", ""),
    )


async def _persist_result(
    conversation_id: str, res: dict, settings: dict,
    user_msg_id: int | None, turn_index: int,
) -> int | None:
    """Persist the assistant message after _result.  Returns the new assistant message id."""
    if settings.get("enable_agent", 1):
        await db.update_director_state(conversation_id, res["active_moods"], res.get("keywords"))
    if res.get("refined_msg") and user_msg_id:
        await db.update_message_content(user_msg_id, res["effective_msg"])

    asst_id = await db.add_message(conversation_id, "assistant", res["resp_text"], turn_index, parent_id=user_msg_id)
    await db.set_active_leaf(conversation_id, asst_id)
    return asst_id


async def _fallback_persist(
    conversation_id: str, res: dict, settings: dict,
    user_msg_id: int | None, turn_index: int, accumulated_text: str,
):
    """Best-effort save when the pipeline aborted before _result was consumed."""
    try:
        if res.get("active_moods") and settings.get("enable_agent", 1):
            await db.update_director_state(conversation_id, res["active_moods"], res.get("keywords"))
        if res.get("refined_msg") and user_msg_id:
            await db.update_message_content(user_msg_id, res["effective_msg"])
        resp_text = res.get("resp_text", "") or accumulated_text
        if resp_text.strip():
            asst_id = await db.add_message(conversation_id, "assistant", resp_text, turn_index, parent_id=user_msg_id)
            await db.set_active_leaf(conversation_id, asst_id)
            logger.info("Fallback persistence saved incomplete assistant message (%d chars)", len(resp_text))
    except Exception:
        logger.exception("Fallback persistence failed")


async def _shielded_fallback(
    conversation_id: str, res: dict, settings: dict,
    user_msg_id: int | None, turn_index: int, accumulated_text: str,
):
    """Run _fallback_persist inside asyncio.shield, with a retry on CancelledError."""
    try:
        await asyncio.shield(
            _fallback_persist(conversation_id, res, settings, user_msg_id, turn_index, accumulated_text)
        )
    except asyncio.CancelledError:
        try:
            await _fallback_persist(conversation_id, res, settings, user_msg_id, turn_index, accumulated_text)
        except Exception:
            logger.exception("Fallback persistence retry failed")


async def _consume_pipeline(
    pipeline: AsyncIterator[dict],
    conversation_id: str, settings: dict,
    user_msg_id: int | None, turn_index: int,
    *,
    extra_on_result=None,
) -> AsyncIterator[dict]:
    """Shared event-consumption loop used by both handle_turn and handle_regenerate.

    *extra_on_result* is an optional async callback ``(res, asst_id) -> None``
    that runs right after the assistant message is persisted (for handle_turn's
    conversation-log write, for example).
    """
    res: dict = {}
    asst_id = None
    persisted = False
    accumulated_text = ""

    try:
        async for event in pipeline:
            etype = event["event"]
            if etype == "token":
                accumulated_text += event["data"]
                yield event
            elif etype == "_result":
                res = event["data"]
                asst_id = await _persist_result(conversation_id, res, settings, user_msg_id, turn_index)
                persisted = True
                if extra_on_result:
                    await extra_on_result(res, asst_id)
            elif etype == "_refined_result":
                res["resp_text"] = event["data"]["resp_text"]
                if asst_id:
                    await db.update_message_content(asst_id, res["resp_text"])
            else:
                yield event
    finally:
        if not persisted:
            await _shielded_fallback(conversation_id, res, settings, user_msg_id, turn_index, accumulated_text)

    yield {"event": "done"}


# ═══════════════════════════════════════════════════════════════════════
# Public entry points
# ═══════════════════════════════════════════════════════════════════════

async def handle_turn(conversation_id: str, user_message: str, skip_user_persist: bool = False) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}; return

        settings = ctx["settings"]
        messages = await db.get_messages(conversation_id)
        conv = ctx["conv"]

        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]

        # Save user message BEFORE pipeline
        if not skip_user_persist:
            user_msg_id = await db.add_message(conversation_id, "user", user_message, next_turn, parent_id=user_parent_id)
            await db.set_active_leaf(conversation_id, user_msg_id)

        prefix = _build_prefix_from_ctx(ctx, history)
        asst_turn = next_turn + (0 if skip_user_persist else 1)

        async def _on_result(res, asst_id):
            await db.add_conversation_log(
                conversation_id, next_turn, res["agent_raw"], res["calls"],
                res["active_moods"], res["inj_block"], res["latency"],
            )

        pipeline = _run_pipeline(ctx["client"], settings, ctx["director"], ctx["fragments"], prefix, user_message, ctx["phrase_bank"])
        async for event in _consume_pipeline(pipeline, conversation_id, settings, user_msg_id, asst_turn, extra_on_result=_on_result):
            yield event

    except Exception as e:
        logger.exception("Pipeline error")
        yield {"event": "error", "data": str(e)}


async def handle_regenerate(conversation_id: str, assistant_msg_id: int) -> AsyncIterator[dict]:
    try:
        ctx = await _load_pipeline_context(conversation_id)
        if ctx is None:
            yield {"event": "error", "data": "Conversation not found"}; return

        settings = ctx["settings"]
        target = await db.get_message_by_id(assistant_msg_id)
        if not target or target["conversation_id"] != conversation_id or target["role"] != "assistant":
            yield {"event": "error", "data": "Invalid target message"}; return

        user_msg_id = target["parent_id"]
        user_msg = await db.get_message_by_id(user_msg_id) if user_msg_id else None
        if not user_msg:
            yield {"event": "error", "data": "Parent user message not found"}; return

        history = await db._get_path_to_leaf(conversation_id, user_msg.get("parent_id")) if user_msg.get("parent_id") else []
        prefix = _build_prefix_from_ctx(ctx, history)

        pipeline = _run_pipeline(ctx["client"], settings, ctx["director"], ctx["fragments"], prefix, user_msg["content"], ctx["phrase_bank"])
        async for event in _consume_pipeline(pipeline, conversation_id, settings, user_msg_id, target["turn_index"]):
            yield event

    except Exception as e:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": str(e)}