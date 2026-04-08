from __future__ import annotations
import json
import logging
import time
from typing import AsyncIterator, Optional

from . import database as db
from .llm_client import LLMClient, parse_tool_calls

logger = logging.getLogger(__name__)

# --- Agent tool definitions (OpenAI function-calling format) ---

AGENT_TOOLS = [{
    "type": "function",
    "function": {
        "name": "set_writing_styles",
        "description": "Set the active writing styles for the next response. Replaces the full set — any style not listed is deactivated. Aim to keep things fresh — consider shifting and combining styles that fit the current mood or scene. May churn and be random. If a style has been used too much, just switch.",
        "parameters": {
            "type": "object",
            "properties": {
                "style_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of style fragment IDs to activate (e.g. ['tense']).",
                }
            },
            "required": ["style_ids"],
        },
    },
}]

REWRITE_PROMPT_TOOL = {
    "type": "function",
    "function": {
        "name": "rewrite_user_prompt",
        "description": "Rewrite the user's message into a more detailed, immersive, action or dialogue. Use ONLY when the input is too short or vague (e.g. \"I laugh\", \"Sure.\", \"I nod\") to generate a compelling response. Write 2 sentences max. If the message is already detailed enough, keep refined_message empty.",
        "parameters": {
            "type": "object",
            "properties": {
                "refined_message": {
                    "type": "string",
                    "description": "An improved, more detailed version of the user's message, written in first person from the user's perspective. Leave empty or omit if no changes are needed.",
                },
            },
            "required": [],
        },
    },
}

REFINE_OUTPUT_TOOL = {
    "type": "function",
    "function": {
        "name": "refine_assistant_output",
        "description": "Audit your previous response for: inconsistencies, anachronisms, repeated phrases or repetitive sentence structures, sloppy/purple prose, avoidant;pretentious words like 'purr', 'predatory', 'velvety', 'ozone', 'core', 'electric' (adj), 'primal', 'mischievous', 'conspiratorial', 'challenge', cliched writing tropes that are variations of 'low, dangerous voice'; 'voice dripping'; 'voice dropping'; 'very brave or very stupid'; 'tension in the air'; 'a mix of...'. Make only the necessary inline fixes by rephrasing or removing. If the output is clean, keep refined_output empty.",
        "parameters": {
            "type": "object",
            "properties": {
                "refined_output": {
                    "type": "string",
                    "description": "The corrected output with targeted fixes applied. Leave empty or omit if no changes are needed.",
                },
            },
            "required": [],
        },
    },
}

TOOLS: dict[str, dict] = {
    "set_writing_styles": {"choice": {"type": "function", "function": {"name": "set_writing_styles"}}, "schema": AGENT_TOOLS[0]},
    "rewrite_user_prompt": {"choice": {"type": "function", "function": {"name": "rewrite_user_prompt"}}, "schema": REWRITE_PROMPT_TOOL},
    "refine_assistant_output": {"choice": {"type": "function", "function": {"name": "refine_assistant_output"}}, "schema": REFINE_OUTPUT_TOOL},
}

POST_WRITER_TOOLS = {"refine_assistant_output"}
ALL_SCHEMAS = [t["schema"] for t in TOOLS.values()]


def _enabled_schemas(enabled_tools: dict | None) -> list[dict]:
    """Compute the tool schemas list from enabled_tools. Returns [] when disabled."""
    if enabled_tools is None:
        return ALL_SCHEMAS
    return [TOOLS[n]["schema"] for n in TOOLS if enabled_tools.get(n, False)]


def build_tool_prompt(tool_name: str, user_message: str, active_styles: list[str], fragments: list[dict]) -> str:
    tool = TOOLS.get(tool_name)
    if not tool:
        return ""
    desc = tool["schema"]["function"]["description"]
    parts = [
        "[OOC] You (the AI) are now the agentic Director, use tool calls to accomplish your task. Your output will immediately affect how the scenario plays out. Be decisive and avoid overthinking.",
        f"Call this tool ONLY: '{tool_name}' - {desc}"
    ]
    if tool_name == "set_writing_styles":
        styles = ", ".join(active_styles) or "none"
        frags = "\n".join(f"- {f['id']}: {f['description']}" for f in fragments)
        parts.append(f"Currently active styles: {styles}\n\nAvailable writing styles:\n{frags}")
        parts.append(f"User's latest message (for context only — do not respond to it):\n\"\"\"{user_message}\"\"\"")
    elif tool_name == "rewrite_user_prompt":
        parts.append(f"User's latest message:\n\"\"\"[{user_message}]\"\"\"")
    return "\n\n".join(parts)


def build_style_injection(active: list[dict], deactivated: list[dict] | None = None) -> str:
    parts = ["<current_scene_direction>"]
    for f in active:
        parts += [f'  <style name="{f["id"]}">', f'    {f["prompt_text"]}', "  </style>"]
    for f in (deactivated or []):
        if neg := f.get("negative_prompt", "").strip():
            parts += [f'  <style name="{f["id"]}" deactivated="true">', f'    {neg}', "  </style>"]
    parts.append("</current_scene_direction>")
    return "\n".join(parts)


def _sub(t: str, user_name: str, char_name: str) -> str:
    return (t or "").replace("{{user}}", user_name or "User").replace("{{char}}", char_name or "Character")


def build_prefix(
    system_prompt: str, char_name: str, char_persona: str, char_scenario: str,
    mes_example: str = "", post_history_instructions: str = "", messages: list[dict] = None,
    user_name: str = "User", user_description: str = "",
) -> list[dict]:
    s = lambda t: _sub(t, user_name, char_name)
    parts = [s(system_prompt)]
    if char_name: parts.append(f"\n\n## Character: {char_name}")
    if char_persona: parts.append(f"\n{s(char_persona)}")
    if char_scenario: parts.append(f"\n\n## Scenario\n{s(char_scenario)}")
    if mes_example: parts.append(f"\n\n## Example Dialogue\n{s(mes_example)}")
    if post_history_instructions: parts.append(f"\n\n## Additional Instructions\n{s(post_history_instructions)}")
    if user_description: parts.append(f"\n\n## User: {user_name or 'User'}\n{user_description}")
    return [{"role": "system", "content": "".join(parts)}] + [{"role": m["role"], "content": m["content"]} for m in (messages or [])]


def apply_tool_calls(tool_calls: list[dict], current_styles: list[str]) -> tuple[list[str], str | None]:
    styles, refined = list(current_styles), None
    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "set_writing_styles":
            styles = args.get("style_ids", [])
        elif tc["name"] == "rewrite_user_prompt":
            refined = args.get("refined_message") or None
    return styles, refined


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


async def _writer_pass(client: LLMClient, msgs: list[dict], settings: dict, enabled_tools: dict | None = None) -> AsyncIterator[dict]:
    params = {k: v for k in ["temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty"] if (v := settings.get(k)) is not None}
    schemas = _enabled_schemas(enabled_tools)
    extra = {"tools": schemas, "tool_choice": "none"} if schemas else {}
    async for token in client.stream(messages=msgs, model=settings["model_name"], **extra, **params):
        yield token


async def _agent_pass(
    client: LLMClient, prefix: list[dict], user_message: str, settings: dict,
    director: dict, fragments: list[dict], enabled_tools: dict | None = None
) -> tuple[list[str], str, list, int, str | None]:
    active_styles, refined_msg, all_calls, last_raw = director["active_styles"], None, [], ""
    tool_names = ["set_writing_styles"] if enabled_tools is None else [
        n for n, on in enabled_tools.items() if on and n in TOOLS and n not in POST_WRITER_TOOLS
    ]
    if not tool_names:
        return active_styles, "", [], 0, None

    tool_schemas = _enabled_schemas(enabled_tools)

    t0 = time.monotonic()
    for name in tool_names:
        msgs = prefix + [{"role": "user", "content": build_tool_prompt(name, user_message, active_styles, fragments)}]
        logger.info("Agent tool=%s prompt:\n%s", name, json.dumps(msgs, indent=2, ensure_ascii=False))
        try:
            resp = await client.complete(
                messages=msgs, model=settings["model_name"], tools=tool_schemas,
                tool_choice=TOOLS[name]["choice"], temperature=0.25, max_tokens=2048
            )
            last_raw = json.dumps(resp, default=str)
            logger.info("Agent tool=%s output:\n%s", name, last_raw)
            if parsed := parse_tool_calls(resp):
                all_calls.extend(parsed)
                active_styles, new_refined = apply_tool_calls(parsed, active_styles)
                if new_refined:
                    refined_msg = new_refined
            else:
                logger.info("Agent tool=%s: model skipped", name)
        except Exception as e:
            logger.error("Agent tool=%s failed: %s", name, e)
            last_raw = f"ERROR: {e}"

    return active_styles, last_raw, all_calls, int((time.monotonic() - t0) * 1000), refined_msg


async def _refine_pass(
    client: LLMClient, prefix: list[dict], effective_msg: str, draft: str, settings: dict
) -> tuple[str | None, str, int]:
    t0 = time.monotonic()
    msgs = prefix + [
        {"role": "user", "content": effective_msg},
        {"role": "assistant", "content": draft},
        {"role": "user", "content": build_tool_prompt("refine_assistant_output", "", [], [])},
    ]
    logger.info("Refine prompt:\n%s", json.dumps(msgs, indent=2, ensure_ascii=False))
    refine_schema = [TOOLS["refine_assistant_output"]["schema"]]
    try:
        resp = await client.complete(
            messages=msgs, model=settings["model_name"], tools=refine_schema,
            tool_choice=TOOLS["refine_assistant_output"]["choice"], temperature=0.25, max_tokens=4096
        )
        raw = json.dumps(resp, default=str)
        logger.info("Refine output:\n%s", raw)
        if parsed := parse_tool_calls(resp):
            for tc in parsed:
                if tc["name"] == "refine_assistant_output":
                    return tc.get("arguments", {}).get("refined_output") or None, raw, int((time.monotonic() - t0) * 1000)
        logger.info("Refine: model skipped")
        return None, raw, int((time.monotonic() - t0) * 1000)
    except Exception as e:
        logger.error("Refine failed: %s", e)
        return None, f"ERROR: {e}", int((time.monotonic() - t0) * 1000)

async def _run_pipeline(
    client: LLMClient, settings: dict, director: dict, fragments: list[dict],
    prefix: list[dict], user_message: str,
) -> AsyncIterator[dict]:
    # Resolve enabled tools; disable all if agent is off
    enabled_tools = settings.get("enabled_tools") or {}
    agent_on = bool(settings.get("enable_agent", 1))
    if not agent_on:
        enabled_tools = {}

    active_styles, agent_raw, calls, latency, refined_msg = director["active_styles"], "", [], 0, None
    effective_msg = user_message
    do_refine = agent_on and enabled_tools.get("refine_assistant_output", False)

    # --- Agent pass: style selection + prompt rewrite ---
    if agent_on:
        yield {"event": "director_start"}
        active_styles, agent_raw, calls, latency, refined_msg = await _agent_pass(
            client, prefix, user_message, settings, director, fragments, enabled_tools
        )
        if refined_msg:
            effective_msg = refined_msg
            yield {"event": "prompt_rewritten", "data": {"refined_message": refined_msg}}

    # Build style injection block from active + newly deactivated styles
    deactivated = [f for f in fragments if f["id"] in (set(director["active_styles"]) - set(active_styles))]
    active = [f for f in fragments if f["id"] in active_styles]
    inj_block = build_style_injection(active, deactivated) if (active or deactivated) else ""

    yield {"event": "director_done", "data": {"active_styles": active_styles, "injection_block": inj_block, "tool_calls": calls, "agent_latency_ms": latency}}

    # --- Writer pass: stream the story response ---
    writer_msgs = prefix + ([{"role": "user", "content": inj_block}] if inj_block else []) + [
        {"role": "user", "content": effective_msg + "\n\n[OOC: Only write the story, tool calls are STRICTLY FORBIDDEN from now on!]"}
    ]

    resp_text = ""
    async for token in _writer_pass(client, writer_msgs, settings, enabled_tools):
        resp_text += token
        yield {"event": "token", "data": token}

    # Yield base result early so caller can persist before refinement
    yield {"event": "_result", "data": {
        "active_styles": active_styles, "agent_raw": agent_raw, "calls": calls,
        "latency": latency, "refined_msg": refined_msg, "effective_msg": effective_msg,
        "resp_text": resp_text, "inj_block": inj_block
    }}

    # --- Refine pass: optional self-audit of the draft ---
    if do_refine and resp_text:
        try:
            refined_output, _, _ = await _refine_pass(client, prefix, effective_msg, resp_text, settings)
            if refined_output:
                resp_text = refined_output
                yield {"event": "writer_rewrite", "data": {"refined_text": resp_text}}
                # Update caller's persisted text if refinement succeeded
                yield {"event": "_refined_result", "data": {"resp_text": resp_text}}
        except Exception as e:
            logger.error("refine pass failed, keeping original: %s", e)


async def handle_turn(conversation_id: str, user_message: str, skip_user_persist: bool = False) -> AsyncIterator[dict]:
    try:
        settings = await db.get_settings()
        conv = await db.get_conversation(conversation_id)
        if not conv:
            yield {"event": "error", "data": "Conversation not found"}; return

        messages = await db.get_messages(conversation_id)
        director = await db.get_director_state(conversation_id)
        fragments = await db.get_fragments()
        client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

        # Determine history and turn indexing
        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        # When skipping persist, pop the last user message out of history
        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]

        # Build the system + history prefix (optimised for KV cache reuse)
        system_prompt, char_persona, mes_example = await _load_char_context(conv, settings)
        prefix = build_prefix(
            system_prompt, conv["character_name"], char_persona,
            conv["character_scenario"], mes_example, conv.get("post_history_instructions", ""),
            history, settings.get("user_name", "User"), settings.get("user_description", "")
        )

        # Run the full agent → writer → refine pipeline
        res = {}
        try:
            async for event in _run_pipeline(client, settings, director, fragments, prefix, user_message):
                if event["event"] == "_result":
                    res = event["data"]
                elif event["event"] == "_refined_result":
                    res["resp_text"] = event["data"]["resp_text"]
                else:
                    yield event
        finally:
            # Always persist whatever we have — guards against disconnect during refinement
            if not res:
                return

            if settings.get("enable_agent", 1):
                await db.update_director_state(conversation_id, res["active_styles"])

            if not skip_user_persist:
                user_msg_id = await db.add_message(conversation_id, "user", res["effective_msg"], next_turn, parent_id=user_parent_id)
                await db.set_active_leaf(conversation_id, user_msg_id)
                asst_id = await db.add_message(conversation_id, "assistant", res["resp_text"], next_turn + 1, parent_id=user_msg_id)
            else:
                if res["refined_msg"] and user_msg_id:
                    await db.update_message_content(user_msg_id, res["refined_msg"])
                asst_id = await db.add_message(conversation_id, "assistant", res["resp_text"], next_turn, parent_id=user_msg_id)

            await db.set_active_leaf(conversation_id, asst_id)
            await db.add_conversation_log(conversation_id, next_turn, res["agent_raw"], res["calls"], res["active_styles"], res["inj_block"], res["latency"])

        yield {"event": "done"}
    except Exception as e:
        logger.exception("Pipeline error")
        yield {"event": "error", "data": str(e)}


async def handle_regenerate(conversation_id: str, assistant_msg_id: int) -> AsyncIterator[dict]:
    try:
        settings = await db.get_settings()
        conv = await db.get_conversation(conversation_id)
        if not conv:
            yield {"event": "error", "data": "Conversation not found"}; return

        # Validate the target assistant message
        target = await db.get_message_by_id(assistant_msg_id)
        if not target or target["conversation_id"] != conversation_id or target["role"] != "assistant":
            yield {"event": "error", "data": "Invalid target message"}; return

        # Walk back to the parent user message
        user_msg_id = target["parent_id"]
        user_msg = await db.get_message_by_id(user_msg_id) if user_msg_id else None
        if not user_msg:
            yield {"event": "error", "data": "Parent user message not found"}; return

        # Rebuild history up to the user message's parent (excluding that turn)
        history = await db._get_path_to_leaf(conversation_id, user_msg.get("parent_id")) if user_msg.get("parent_id") else []
        director = await db.get_director_state(conversation_id)
        # Roll back styles to what they were before this turn
        prev_styles = await db.get_styles_before_turn(conversation_id, user_msg["turn_index"])
        director = {**director, "active_styles": prev_styles}
        fragments = await db.get_fragments()
        client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

        # Build prefix from the rolled-back history
        system_prompt, char_persona, mes_example = await _load_char_context(conv, settings)
        prefix = build_prefix(
            system_prompt, conv["character_name"], char_persona,
            conv["character_scenario"], mes_example, conv.get("post_history_instructions", ""),
            history, settings.get("user_name", "User"), settings.get("user_description", "")
        )

        # Re-run the pipeline with the original user message
        res = {}
        try:
            async for event in _run_pipeline(client, settings, director, fragments, prefix, user_msg["content"]):
                if event["event"] == "_result":
                    res = event["data"]
                elif event["event"] == "_refined_result":
                    res["resp_text"] = event["data"]["resp_text"]
                else:
                    yield event
        finally:
            # Always persist — guards against disconnect during refinement
            if not res:
                return

            if settings.get("enable_agent", 1):
                await db.update_director_state(conversation_id, res["active_styles"])
                if res["refined_msg"]:
                    await db.update_message_content(user_msg_id, res["refined_msg"])

            new_asst_id = await db.add_message(conversation_id, "assistant", res["resp_text"], target["turn_index"], parent_id=user_msg_id)
            await db.set_active_leaf(conversation_id, new_asst_id)

        yield {"event": "done"}
    except Exception as e:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": str(e)}