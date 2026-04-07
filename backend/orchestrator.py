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
        "description": "Set the active writing styles for the next response. Replaces the full set — any style not listed is deactivated. Aim to keep things fresh — consider shifting and combining styles that fit the current mood or scene. If the current styles are already ideal, keep them as is.",
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
        "description": "Rewrite the user's message into a more detailed, immersive, action or dialogue. Use ONLY when the input is too short or vague (e.g. \"I laugh\", \"Sure.\", \"I nod\") to generate a compelling response. Write 2 sentences max. If the message is already detailed enough, keep rewritten_message empty.",
        "parameters": {
            "type": "object",
            "properties": {
                "rewritten_message": {
                    "type": "string",
                    "description": "An improved, more detailed version of the user's message, written in first person from the user's perspective. Leave empty or omit if no changes are needed.",
                },
            },
            "required": [],
        },
    },
}

FIX_WRITER_OUTPUT_TOOL = {
    "type": "function",
    "function": {
        "name": "fix_writer_output",
        "description": "Audit the preceding assistant's response for: anachronisms, repeated phrases or sentence structures, sloppy/purple prose, avoidant;pretentious words like 'purr', 'predatory', 'velvety', 'ozone', 'heat', 'core', 'electric' (adj), 'primal', 'mischievous', 'conspiratorial', 'challenge', cliched writing tropes similar to 'low, dangerous voice'; 'voice dripping'; 'voice dropping'; 'tension in the air', 'a mixture of...'. Make only the necessary inline fixes by rephrasing or removing. If the output is clean, keep rewritten_output empty.",
        "parameters": {
            "type": "object",
            "properties": {
                "rewritten_output": {
                    "type": "string",
                    "description": "The corrected output with targeted fixes applied. Leave empty or omit if no changes are needed.",
                },
            },
            "required": [],
        },
    },
}

ALL_TOOL_DEFS: dict[str, dict] = {
    "set_writing_styles": {"tool_choice": {"type": "function", "function": {"name": "set_writing_styles"}}, "schema": AGENT_TOOLS[0]},
    "rewrite_user_prompt": {"tool_choice": {"type": "function", "function": {"name": "rewrite_user_prompt"}}, "schema": REWRITE_PROMPT_TOOL},
    "fix_writer_output": {"tool_choice": {"type": "function", "function": {"name": "fix_writer_output"}}, "schema": FIX_WRITER_OUTPUT_TOOL},
}

# Tools that run after the writer, not during the director pass.
POST_WRITER_TOOLS = {"fix_writer_output"}



def build_tool_prompt(tool_name: str, user_message: str, active_styles: list[str], available_fragments: list[dict]) -> str:
    """Build a focused, optional-use prompt for a single agent tool, reusing its schema description."""
    tool_def = ALL_TOOL_DEFS.get(tool_name)
    if not tool_def:
        return ""

    desc = tool_def["schema"]["function"]["description"]
    parts = [
        "[OOC] You (the AI) are now the Director, use tool calls to accomplish your task. Your direction will immediately affect how the scenario plays out. Be decisive.",
        f"Allowed tool - only call this: '{tool_name}' - {desc}"
    ]
    
    if tool_name == "set_writing_styles":
        styles = ", ".join(active_styles) or "none"
        frags = "\n".join(f"- {f['id']}: {f['description']}" for f in available_fragments)
        parts.append(f"Currently active styles: {styles}\n\nAvailable writing styles:\n{frags}")
        parts.append(f"User's latest message (for context only — do not respond to it):\n\"\"\"{user_message}\"\"\"")
    elif tool_name == "rewrite_user_prompt":
        parts.append(f"User's latest message:\n\"\"\"[{user_message}]\"\"\"")
        
    return "\n\n".join(parts)


def build_injection_block(active_fragments: list[dict], deactivated_fragments: Optional[list[dict]] = None) -> str:
    """Assemble the <current_scene_direction> XML block for depth-0.5 injection."""
    parts = ["<current_scene_direction>"]
    for f in active_fragments:
        parts.extend([f'  <style name="{f["id"]}">', f'    {f["prompt_text"]}', "  </style>"])
    for f in (deactivated_fragments or []):
        if neg := f.get("negative_prompt", "").strip():
            parts.extend([f'  <style name="{f["id"]}" deactivated="true">', f'    {neg}', "  </style>"])
    parts.append("</current_scene_direction>")
    return "\n".join(parts)


def build_shared_prefix(
    system_prompt: str, character_name: str, character_persona: str, character_scenario: str,
    mes_example: str = "", post_history_instructions: str = "", messages: list[dict] = None,
    user_name: str = "User", user_description: str = "",
) -> list[dict]:
    """Build the shared prompt prefix used by both agent and writer passes."""
    def sub(t: str) -> str:
        return (t or "").replace("{{user}}", user_name or "User").replace("{{char}}", character_name or "Character")

    parts = [sub(system_prompt)]
    if character_name: parts.append(f"\n\n## Character: {character_name}")
    if character_persona: parts.append(f"\n{sub(character_persona)}")
    if character_scenario: parts.append(f"\n\n## Scenario\n{sub(character_scenario)}")
    if mes_example: parts.append(f"\n\n## Example Dialogue\n{sub(mes_example)}")
    if post_history_instructions: parts.append(f"\n\n## Additional Instructions\n{sub(post_history_instructions)}")
    if user_description: parts.append(f"\n\n## User: {user_name or 'User'}\n{user_description}")

    return [{"role": "system", "content": "".join(parts)}] + [{"role": m["role"], "content": m["content"]} for m in (messages or [])]


def apply_tool_calls(tool_calls: list[dict], current_styles: list[str]) -> tuple[list[str], Optional[str]]:
    """Apply parsed tool calls to director state."""
    new_styles, rewritten = list(current_styles), None
    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "set_writing_styles":
            new_styles = args.get("style_ids", [])
        elif tc["name"] == "rewrite_user_prompt":
            rewritten = args.get("rewritten_message") or None
    return new_styles, rewritten


async def _run_writer_pass(client: LLMClient, msgs: list[dict], settings: dict, enabled_tools: Optional[dict] = None) -> AsyncIterator[dict]:
    """Run the writer pass (streaming). Yields content deltas. Tools are sent to match the KV cache prefix used by agent/rewrite passes."""
    params = {k: v for k in ["temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty"] if (v := settings.get(k)) is not None}
    if enabled_tools is None:
        tool_schemas = [v["schema"] for v in ALL_TOOL_DEFS.values()]
    else:
        tool_schemas = [ALL_TOOL_DEFS[n]["schema"] for n in ALL_TOOL_DEFS if enabled_tools.get(n, False)]
    extra = {"tools": tool_schemas, "tool_choice": "none"} if tool_schemas else {}
    async for token in client.stream(messages=msgs, model=settings["model_name"], **extra, **params):
        yield token


async def _run_agent_pass(
    client: LLMClient, prefix: list[dict], user_message: str, settings: dict,
    director: dict, fragments: list[dict], enabled_tools: Optional[dict] = None
) -> tuple[list[str], str, list, int, Optional[str]]:
    """Run the agent pass by iterating each enabled tool individually."""
    active_styles, rewritten_message, all_calls, last_raw = director["active_styles"], None, [], ""
    tool_names = ["set_writing_styles"] if enabled_tools is None else [n for n, on in enabled_tools.items() if on and n in ALL_TOOL_DEFS and n not in POST_WRITER_TOOLS]
    
    if not tool_names:
        return active_styles, "", [], 0, None

    t0 = time.monotonic()
    for name in tool_names:
        msgs = prefix + [{"role": "user", "content": build_tool_prompt(name, user_message, active_styles, fragments)}]
        logger.info("Agent tool=%s prompt:\n%s", name, json.dumps(msgs, indent=2, ensure_ascii=False))

        try:
            resp = await client.complete(
                messages=msgs, model=settings["model_name"], tools=[v["schema"] for v in ALL_TOOL_DEFS.values()],
                tool_choice=ALL_TOOL_DEFS[name]["tool_choice"], temperature=0.25, max_tokens=2048
            )
            last_raw = json.dumps(resp, default=str)
            logger.info("Agent tool=%s output:\n%s", name, last_raw)

            if parsed_calls := parse_tool_calls(resp):
                all_calls.extend(parsed_calls)
                active_styles, new_rewr = apply_tool_calls(parsed_calls, active_styles)
                if new_rewr: rewritten_message = new_rewr
            else:
                logger.info("Agent tool=%s: model skipped", name)
        except Exception as e:
            logger.error("Agent tool=%s failed: %s", name, e)
            last_raw = f"ERROR: {e}"

    return active_styles, last_raw, all_calls, int((time.monotonic() - t0) * 1000), rewritten_message


async def _run_writer_rewrite_pass(
    client: LLMClient, prefix: list[dict], eff_msg: str, resp_text: str, settings: dict
) -> tuple[Optional[str], str, int]:
    """Run the post-writer writer audit pass. Returns (rewritten_text_or_none, raw_resp, latency_ms).
    Message structure: history + user turn + writer draft + OOC audit instruction."""
    t0 = time.monotonic()
    msgs = prefix + [
        {"role": "user", "content": eff_msg},
        {"role": "assistant", "content": resp_text},
        {"role": "user", "content": build_tool_prompt("fix_writer_output", "", [], [])},
    ]
    logger.info("Writer rewrite prompt:\n%s", json.dumps(msgs, indent=2, ensure_ascii=False))
    try:
        resp = await client.complete(
            messages=msgs, model=settings["model_name"], tools=[v["schema"] for v in ALL_TOOL_DEFS.values()],
            tool_choice=ALL_TOOL_DEFS["fix_writer_output"]["tool_choice"], temperature=0.25, max_tokens=4096
        )
        raw = json.dumps(resp, default=str)
        logger.info("Writer rewrite output:\n%s", raw)
        if parsed_calls := parse_tool_calls(resp):
            for tc in parsed_calls:
                if tc["name"] == "fix_writer_output":
                    rewritten = tc.get("arguments", {}).get("rewritten_output") or None
                    return rewritten, raw, int((time.monotonic() - t0) * 1000)
        logger.info("Writer rewrite: model skipped")
        return None, raw, int((time.monotonic() - t0) * 1000)
    except Exception as e:
        logger.error("Writer rewrite failed: %s", e)
        return None, f"ERROR: {e}", int((time.monotonic() - t0) * 1000)


async def _execute_pipeline(
    client: LLMClient, settings: dict, director: dict, fragments: list[dict],
    prefix: list[dict], user_message: str,
) -> AsyncIterator[dict]:
    """Common logic for the LLM execution pipeline shared by handles."""
    enabled_tools = settings.get("enabled_tools") or {}
    enable_agent = bool(settings.get("enable_agent", 1))
    if not enable_agent:
        enabled_tools = {}
    act_styles, agent_raw, calls, latency, rewr_msg = director["active_styles"], "", [], 0, None
    eff_msg = user_message
    writer_rewrite_enabled = enable_agent and enabled_tools.get("fix_writer_output", False)

    if enable_agent:
        yield {"event": "director_start"}
        act_styles, agent_raw, calls, latency, rewr_msg = await _run_agent_pass(
            client, prefix, user_message, settings, director, fragments, enabled_tools
        )
        if rewr_msg:
            eff_msg = rewr_msg
            yield {"event": "prompt_rewritten", "data": {"rewritten_message": rewr_msg}}

    deactivated = [f for f in fragments if f["id"] in (set(director["active_styles"]) - set(act_styles))]
    active = [f for f in fragments if f["id"] in act_styles]
    inj_block = build_injection_block(active, deactivated) if (active or deactivated) else ""

    yield {"event": "director_done", "data": {"active_styles": act_styles, "injection_block": inj_block, "tool_calls": calls, "agent_latency_ms": latency}}

    perf_msgs = prefix + ([{"role": "system", "content": inj_block}] if inj_block else []) + [{"role": "user", "content": eff_msg + "\n\n[OOC: Only write the story, tool calls are STRICTLY FORBIDDEN from now on because they are extremely DESTRUCTIVE!]"}]
    
    resp_text = ""
    async for token in _run_writer_pass(client, perf_msgs, settings, enabled_tools if enabled_tools else None):
        resp_text += token
        yield {"event": "token", "data": token}

    if writer_rewrite_enabled and resp_text:
        rewritten_output, _, _ = await _run_writer_rewrite_pass(client, prefix, eff_msg, resp_text, settings)
        if rewritten_output:
            resp_text = rewritten_output
            yield {"event": "writer_rewrite", "data": {"rewritten_text": resp_text}}

    yield {"event": "_pipeline_result", "data": {"act_styles": act_styles, "agent_raw": agent_raw, "calls": calls, "latency": latency, "rewr_msg": rewr_msg, "eff_msg": eff_msg, "resp_text": resp_text, "inj_block": inj_block}}


async def handle_turn(
    conversation_id: str, user_message: str,
    skip_user_persist: bool = False,
) -> AsyncIterator[dict]:
    """Run the full two-pass pipeline for one user turn."""
    try:
        settings = await db.get_settings()
        if not (conv := await db.get_conversation(conversation_id)):
            yield {"event": "error", "data": "Conversation not found"}; return

        messages = await db.get_messages(conversation_id)
        director = await db.get_director_state(conversation_id)
        fragments = await db.get_fragments()
        client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

        history_for_prefix, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history_for_prefix = messages[:-1]
            user_msg_id = messages[-1]["id"]

        system_prompt = settings["system_prompt"]
        character_persona = ""
        mes_example = ""
        if conv.get("character_card_id"):
            card = await db.get_character_card(conv["character_card_id"])
            if card:
                character_persona = "\n\n".join(filter(None, [card.get("description", ""), card.get("personality", "")]))
                mes_example = card.get("mes_example", "")
                if card.get("system_prompt"):
                    system_prompt = card["system_prompt"]

        prefix = build_shared_prefix(
            system_prompt, conv["character_name"], character_persona,
            conv["character_scenario"], mes_example, conv.get("post_history_instructions", ""),
            history_for_prefix, settings.get("user_name", "User"), settings.get("user_description", "")
        )

        enable_agent = bool(settings.get("enable_agent", 1))
        res = {}
        async for event in _execute_pipeline(client, settings, director, fragments, prefix, user_message):
            if event["event"] == "_pipeline_result": res = event["data"]
            else: yield event

        if enable_agent:
            await db.update_director_state(conversation_id, res["act_styles"])

        if not skip_user_persist:
            user_msg_id = await db.add_message(conversation_id, "user", res["eff_msg"], next_turn, parent_id=user_parent_id)
            await db.set_active_leaf(conversation_id, user_msg_id)
            asst_msg_id = await db.add_message(conversation_id, "assistant", res["resp_text"], next_turn + 1, parent_id=user_msg_id)
        else:
            if res["rewr_msg"] and user_msg_id:
                await db.update_message_content(user_msg_id, res["rewr_msg"])
            asst_msg_id = await db.add_message(conversation_id, "assistant", res["resp_text"], next_turn, parent_id=user_msg_id)

        await db.set_active_leaf(conversation_id, asst_msg_id)
        await db.add_conversation_log(conversation_id, next_turn, res["agent_raw"], res["calls"], res["act_styles"], res["inj_block"], res["latency"])
        yield {"event": "done"}
    except Exception as e:
        logger.exception("Pipeline error")
        yield {"event": "error", "data": str(e)}


async def handle_regenerate(
    conversation_id: str, assistant_msg_id: int,
) -> AsyncIterator[dict]:
    """Regenerate a specific assistant message as a new sibling branch."""
    try:
        settings = await db.get_settings()
        if not (conv := await db.get_conversation(conversation_id)):
            yield {"event": "error", "data": "Conversation not found"}; return

        target_asst = await db.get_message_by_id(assistant_msg_id)
        if not target_asst or target_asst["conversation_id"] != conversation_id or target_asst["role"] != "assistant":
            yield {"event": "error", "data": "Invalid target message"}; return

        user_msg_id = target_asst["parent_id"]
        if not (user_msg := await db.get_message_by_id(user_msg_id) if user_msg_id else None):
            yield {"event": "error", "data": "Parent user message not found"}; return

        history_before = await db._get_path_to_leaf(conversation_id, user_msg.get("parent_id")) if user_msg.get("parent_id") else []
        director = await db.get_director_state(conversation_id)
        prev_styles = await db.get_styles_before_turn(conversation_id, user_msg["turn_index"])
        director = {**director, "active_styles": prev_styles}
        fragments = await db.get_fragments()
        client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

        system_prompt = settings["system_prompt"]
        character_persona = ""
        mes_example = ""
        if conv.get("character_card_id"):
            card = await db.get_character_card(conv["character_card_id"])
            if card:
                character_persona = "\n\n".join(filter(None, [card.get("description", ""), card.get("personality", "")]))
                mes_example = card.get("mes_example", "")
                if card.get("system_prompt"):
                    system_prompt = card["system_prompt"]

        prefix = build_shared_prefix(
            system_prompt, conv["character_name"], character_persona,
            conv["character_scenario"], mes_example, conv.get("post_history_instructions", ""),
            history_before, settings.get("user_name", "User"), settings.get("user_description", "")
        )

        enable_agent = bool(settings.get("enable_agent", 1))
        res = {}
        async for event in _execute_pipeline(client, settings, director, fragments, prefix, user_msg["content"]):
            if event["event"] == "_pipeline_result": res = event["data"]
            else: yield event

        if enable_agent:
            await db.update_director_state(conversation_id, res["act_styles"])
            if res["rewr_msg"]:
                await db.update_message_content(user_msg_id, res["rewr_msg"])

        new_asst_id = await db.add_message(conversation_id, "assistant", res["resp_text"], target_asst["turn_index"], parent_id=user_msg_id)
        await db.set_active_leaf(conversation_id, new_asst_id)
        yield {"event": "done"}

    except Exception as e:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": str(e)}