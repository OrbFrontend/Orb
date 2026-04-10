from __future__ import annotations
import asyncio
import json
import logging
import time
from typing import AsyncIterator, Optional

from . import database as db
from .llm_client import LLMClient, parse_tool_calls
from .audit import run_audit, format_report

logger = logging.getLogger(__name__)

# --- Agent tool definitions (OpenAI function-calling format) ---

# --- Agent tool definitions (OpenAI function-calling format) ---

AGENT_TOOLS = [{
    "type": "function",
    "function": {
        "name": "direct_scene",
        "description": "Call this to direct the scene. Deduce what the user wants to see and show them. Combine and configure the moods, specify the direction the scene should take, detect and report repetitive tropes, phrases, subjects, and narrative patterns to avoid. Be very specific with the direction.",
        "parameters": {
            "type": "object",
            "properties": {
                "moods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of moods to activate.",
                },
                "plot_direction": {
                    "type": "string",
                    "description": "What happens next in the story — events, actions, reveals, turns of fate (e.g. 'his dad knows he's lying and snaps', 'the attack tears off a piece of her clothing', 'he makes a rude gesture the teacher doesn't see', 'she's leaning on his shoulder'). Keep to one short sentence.",
                },
                "narration_direction": {
                    "type": "string",
                    "description": "How the scene should be written — focus, emphasis, descriptive lens, internal state (e.g. 'describe his anxious tics in detail', 'narrate her spiraling thoughts on why it went wrong', 'describe her exposed body parts vividly', 'narrate what would happen if she saw it', 'describe how stray strands of hair feel against his cheek'). Keep to one short sentence.",
                },
                "detected_repetitions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific tropes, phrases, subjects, or narrative patterns that are recently overused in the narration. Only report the ones that are recent.",
                },
            },
            "required": ["moods", "plot_direction"],
        },
    },
}]

REWRITE_PROMPT_TOOL = {
    "type": "function",
    "function": {
        "name": "rewrite_user_prompt",
        "description": "Rewrite the user's message into a more detailed, immersive, action or dialogue. Use ONLY when the input is too short or vague (e.g. \"I laugh\", \"Sure.\", \"I nod\") to generate a compelling response. Write 2 sentences max, be direct and succinct. If the message is already detailed enough, keep refined_message empty.",
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

REFINE_APPLY_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "refine_apply_patch",
        "description": "Apply one or more exact text replacements to the draft. Each 'search' must exactly match current draft text (case-sensitive, including punctuation). Returns an updated Audit Report.",
        "parameters": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "search": {"type": "string", "description": "Exact text to find in the draft."},
                            "replace": {"type": "string", "description": "Replacement text."},
                        },
                        "required": ["search", "replace"],
                    },
                    "description": "Ordered list of search/replace pairs.",
                }
            },
            "required": ["patches"],
        },
    },
}

REFINE_AGENT_INSTRUCTIONS = (
    "You are the Refinement Agent. Fix every issue listed in the AUDIT REPORT below.\n\n"
    "RULES:\n"
    "- Send ONE `refine_apply_patch` call with one patch per flagged issue.\n"
    "- The `search` field must be copied EXACTLY from the draft text above — including all punctuation and quotes.\n"
    "- Each patch must target a DIFFERENT, non-overlapping piece of text.\n"
    "- Do NOT send a patch where `search` and `replace` are identical.\n"
    "- Keep replacements close in length to the original. Preserve the author's voice.\n"
    "- For banned phrases: rewrite the sentence to remove the phrase entirely.\n"
    "- For repetitive openers: change how the sentence begins.\n"
    "- For repetitive templates: restructure the sentence (reorder clauses, combine, vary syntax)."
)

_MAX_REFINE_ITERATIONS = 3

TOOLS: dict[str, dict] = {
    "direct_scene": {"choice": {"type": "function", "function": {"name": "direct_scene"}}, "schema": AGENT_TOOLS[0]},
    "rewrite_user_prompt": {"choice": {"type": "function", "function": {"name": "rewrite_user_prompt"}}, "schema": REWRITE_PROMPT_TOOL},
    "refine_apply_patch": {"choice": {"type": "function", "function": {"name": "refine_apply_patch"}}, "schema": REFINE_APPLY_PATCH_TOOL},
}

POST_WRITER_TOOLS = {"refine_apply_patch"}
ALL_SCHEMAS = [t["schema"] for t in TOOLS.values()]


def _enabled_schemas(enabled_tools: dict | None) -> list[dict]:
    """Return tool schemas. None means 'all enabled', {} means 'all disabled'."""
    if enabled_tools is None:
        return ALL_SCHEMAS
    return [TOOLS[n]["schema"] for n in TOOLS if enabled_tools.get(n, False)]


def build_tool_prompt(tool_name: str, user_message: str, active_moods: list[str], fragments: list[dict]) -> str:
    tool = TOOLS.get(tool_name)
    if not tool:
        return ""
    desc = tool["schema"]["function"]["description"]
    parts = [
        "[OOC] Pause to improve the roleplay. Use tool calls to accomplish your task. Your output will immediately affect how the scenario plays out. Be decisive and avoid overthinking.",
        f"ONLY call this tool with extreme focus: '{tool_name}' - {desc}"
    ]
    if tool_name == "direct_scene":
        moods = ", ".join(active_moods) or "none"
        frags = "\n".join(f"- [{f['id']}] - use in case: {f['description']}" for f in fragments)
        parts.append(f"Currently active moods: {moods}\n\nAvailable writing moods:\n{frags}")
        parts.append(f"User's latest message (for context only — do not respond to it):\n\"\"\"{user_message}\"\"\"")
    elif tool_name == "rewrite_user_prompt":
        parts.append(f"User's latest message:\n\"\"\"[{user_message}]\"\"\"")
    return "\n\n".join(parts)


def build_style_injection(
    active: list[dict], deactivated: list[dict] | None = None,
    plot_direction: str | None = None, narration_direction: str | None = None,
    detected_repetitions: list[str] | None = None,
) -> str:
    parts = ["<current_scene_direction>"]
    if plot_direction:
        parts.append(f"  <plot>{plot_direction}</plot>")
    if narration_direction:
        parts.append(f"  <narration>{narration_direction}</narration>")
    if detected_repetitions:
        parts.append("  <avoid>")
        for phrase in detected_repetitions:
            parts.append(f"    - {phrase}")
        parts.append("  </avoid>")
    for f in active:
        parts += [f'  <mood name="{f["id"]}">', f'    {f["prompt_text"]}', "  </mood>"]
    for f in (deactivated or []):
        if neg := f.get("negative_prompt", "").strip():
            parts += [f'  <mood name="{f["id"]}" deactivated="true">', f'    {neg}', "  </mood>"]
    parts.append("</current_scene_direction>")
    return "\n".join(parts)


def build_prefix(
    system_prompt: str, char_name: str, char_persona: str, char_scenario: str,
    mes_example: str = "", post_history_instructions: str = "", messages: list[dict] = None,
    user_name: str = "User", user_description: str = "",
) -> list[dict]:
    parts = [system_prompt]
    if char_name: parts.append(f"\n\n## Character: {char_name}")
    if char_persona: parts.append(f"\n{char_persona}")
    if char_scenario: parts.append(f"\n\n## Scenario\n{char_scenario}")
    if mes_example: parts.append(f"\n\n## Example Dialogue\n{mes_example}")
    if post_history_instructions: parts.append(f"\n\n## Additional Instructions\n{post_history_instructions}")
    if user_description: parts.append(f"\n\n## User: {user_name or 'User'}\n{user_description}")
    return [{"role": "system", "content": "".join(parts)}] + [{"role": m["role"], "content": m["content"]} for m in (messages or [])]


def apply_tool_calls(tool_calls: list[dict], current_moods: list[str]) -> tuple[list[str], str | None, str | None, str | None, list[str] | None]:
    moods, refined, plot_direction, narration_direction, detected_repetitions = list(current_moods), None, None, None, None
    for tc in tool_calls:
        args = tc.get("arguments", {})
        if tc["name"] == "direct_scene":
            moods = args.get("moods", [])
            plot_direction = args.get("plot_direction") or None
            narration_direction = args.get("narration_direction") or None
            detected_repetitions = args.get("detected_repetitions") or None
        elif tc["name"] == "rewrite_user_prompt":
            refined = args.get("refined_message") or None
    return moods, refined, plot_direction, narration_direction, detected_repetitions


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
) -> tuple[list[str], str, list, int, str | None, str | None, str | None, list[str] | None]:
    active_moods, refined_msg, plot_direction, narration_direction, detected_repetitions, all_calls, last_raw = director["active_moods"], None, None, None, None, [], ""
    tool_names = ["direct_scene"] if enabled_tools is None else [
        n for n, on in enabled_tools.items() if on and n in TOOLS and n not in POST_WRITER_TOOLS
    ]
    if not tool_names:
        return active_moods, "", [], 0, None, None, None, None

    tool_schemas = _enabled_schemas(enabled_tools)

    t0 = time.monotonic()
    for name in tool_names:
        msgs = prefix + [{"role": "user", "content": build_tool_prompt(name, user_message, active_moods, fragments)}]
        logger.info("Agent tool=%s prompt:\n%s", name, json.dumps(msgs, indent=2, ensure_ascii=False))
        try:
            resp = await client.complete(
                messages=msgs, model=settings["model_name"], tools=tool_schemas,
                tool_choice=TOOLS[name]["choice"], temperature=0.25, max_tokens=8192
            )
            last_raw = json.dumps(resp, default=str)
            logger.info("Agent tool=%s output:\n%s", name, last_raw)
            if parsed := parse_tool_calls(resp):
                all_calls.extend(parsed)
                active_moods, new_refined, new_plot, new_narration, new_detected_repetitions = apply_tool_calls(parsed, active_moods)
                if new_refined:
                    refined_msg = new_refined
                if new_plot:
                    plot_direction = new_plot
                if new_narration:
                    narration_direction = new_narration
                if new_detected_repetitions:
                    detected_repetitions = new_detected_repetitions
            else:
                logger.info("Agent tool=%s: model skipped", name)
        except Exception as e:
            logger.error("Agent tool=%s failed: %s", name, e)
            last_raw = f"ERROR: {e}"

    return active_moods, last_raw, all_calls, int((time.monotonic() - t0) * 1000), refined_msg, plot_direction, narration_direction, detected_repetitions


_QUOTE_MAP = str.maketrans({
    "\u201c": '"', "\u201d": '"',  # smart double quotes → straight
    "\u2018": "'", "\u2019": "'",  # smart single quotes → straight
    "\u2013": "-", "\u2014": "-",  # en/em dash → hyphen
})


def _normalize_quotes(text: str) -> str:
    return text.translate(_QUOTE_MAP)


def _apply_patches(draft: str, patches: list[dict]) -> tuple[str, list[str]]:
    """Apply search/replace patches to draft. Returns (updated_draft, error_messages)."""
    errors: list[str] = []
    logger.info("Applying %d patches to draft (%d chars)", len(patches), len(draft))
    for i, p in enumerate(patches):
        search = p.get("search", "")
        replace = p.get("replace", "")
        if not search:
            logger.info("Patch %d: empty search string, skipping", i)
            continue
        if search == replace:
            logger.info("Patch %d: no-op (search === replace), skipping", i)
            continue

        count = draft.count(search)

        # Fallback: try with normalized quotes if exact match fails
        if count == 0:
            norm_search = _normalize_quotes(search)
            norm_draft = _normalize_quotes(draft)
            norm_count = norm_draft.count(norm_search)
            if norm_count == 1:
                # Find the position in normalized space, extract the original substring, replace it
                pos = norm_draft.index(norm_search)
                original_substr = draft[pos : pos + len(norm_search)]
                # Verify length alignment (normalization shouldn't change length for single-char replacements)
                if len(original_substr) == len(norm_search):
                    draft = draft[:pos] + replace + draft[pos + len(original_substr):]
                    logger.info("Patch %d OK (quote-normalized): %r → %r", i, search[:80], replace[:80])
                    continue
                else:
                    logger.warning("Patch %d: quote-norm matched but lengths diverged (%d vs %d), falling through",
                                   i, len(original_substr), len(norm_search))
            elif norm_count > 1:
                err = f"Error: Multiple matches ({norm_count}) for '{search[:80]}' (after quote normalization). Use more context."
                logger.warning("Patch %d AMBIGUOUS after quote-norm (%d matches): search=%r", i, norm_count, search[:120])
                errors.append(err)
                continue

            err = f"Error: '{search[:80]}' not found in draft."
            logger.warning("Patch %d MISS (0 matches, even after quote-norm): search=%r", i, search[:120])
            errors.append(err)
        elif count > 1:
            err = f"Error: Multiple matches ({count}) for '{search[:80]}'. Use more context."
            logger.warning("Patch %d AMBIGUOUS (%d matches): search=%r", i, count, search[:120])
            errors.append(err)
        else:
            draft = draft.replace(search, replace, 1)
            logger.info("Patch %d OK: %r → %r", i, search[:80], replace[:80])
    logger.info("Patch application done: %d errors out of %d patches", len(errors), len(patches))
    return draft, errors


async def _refine_pass(
    client: LLMClient, prefix: list[dict], effective_msg: str, draft: str,
    settings: dict, phrase_bank: list[list[str]],
    enabled_tools: dict | None = None,
) -> tuple[str | None, str, int]:
    """ReAct-style refinement loop with programmatic audit detection.

    Returns (refined_draft_or_None, debug_log, latency_ms).
    """
    t0 = time.monotonic()
    debug_parts: list[str] = []

    # Step 1: Run programmatic scanners
    logger.info("Refine: running audit on draft (%d chars), phrase_bank has %d groups", len(draft), len(phrase_bank))
    report = run_audit(draft, phrase_bank)
    report_text = format_report(report)
    logger.info(
        "Refine: initial audit — %d issues (cliches=%d, openers=%d, templates=%d)",
        report.total_issues, report.cliche_result.flagged_count,
        len(report.monotony_result.flagged_openers), len(report.template_result.flagged_templates),
    )
    logger.info("Refine: initial audit report:\n%s", report_text)
    debug_parts.append(f"Initial audit ({report.total_issues} issues):\n{report_text}")

    if report.is_clean:
        logger.info("Refine: audit clean, skipping LLM loop")
        return None, "\n---\n".join(debug_parts), int((time.monotonic() - t0) * 1000)

    # Step 2: Build message context (reuses KV cache prefix)
    refine_tool_schema = REFINE_APPLY_PATCH_TOOL
    msgs = prefix + [
        {"role": "user", "content": effective_msg},
        {"role": "assistant", "content": draft},
        {"role": "system", "content": REFINE_AGENT_INSTRUCTIONS + "\n\n" + report_text},
    ]
    logger.info("Refine: built message context with %d turns (%d prefix + 3 refine)",
                 len(msgs), len(prefix))

    current_draft = draft
    prev_issues = report.total_issues

    # Step 3: ReAct loop
    for iteration in range(_MAX_REFINE_ITERATIONS):
        logger.info("Refine iteration %d/%d, %d issues remaining, draft=%d chars, thread=%d turns",
                     iteration + 1, _MAX_REFINE_ITERATIONS, report.total_issues,
                     len(current_draft), len(msgs))
        try:
            logger.info("Refine iteration %d: calling LLM (model=%s, max_tokens=4096, temp=0.25)",
                         iteration + 1, settings["model_name"])
            try:
                resp = await client.complete(
                    messages=msgs,
                    model=settings["model_name"],
                    tools=[refine_tool_schema],
                    tool_choice=TOOLS["refine_apply_patch"]["choice"],
                    temperature=0.25,
                    max_tokens=8192,
                )
            except Exception as llm_err:
                logger.error("Refine iteration %d: client.complete() raised %s: %s",
                             iteration + 1, type(llm_err).__name__, llm_err, exc_info=True)
                raise
            logger.info("Refine iteration %d: LLM returned (keys=%s)",
                        iteration + 1, list(resp.keys()) if isinstance(resp, dict) else type(resp).__name__)
            raw = json.dumps(resp, default=str)
            logger.info("Refine iteration %d: raw LLM response:\n%s", iteration + 1, raw[:2000])
            debug_parts.append(f"Iteration {iteration + 1} response:\n{raw}")

            # Log finish reason if present
            finish_reason = resp.get("finish_reason") or resp.get("stop_reason")
            if finish_reason:
                logger.info("Refine iteration %d: finish_reason=%s", iteration + 1, finish_reason)

            parsed = parse_tool_calls(resp)
            logger.info("Refine iteration %d: parse_tool_calls returned %d call(s): %s",
                         iteration + 1, len(parsed),
                         [tc["name"] for tc in parsed] if parsed else "[]")
            if not parsed:
                logger.info("Refine iteration %d: model produced no tool call, stopping", iteration + 1)
                break

            # Find refine_apply_patch call
            patch_call = next((tc for tc in parsed if tc["name"] == "refine_apply_patch"), None)
            if not patch_call:
                logger.info("Refine iteration %d: no refine_apply_patch in calls %s, stopping",
                            iteration + 1, [tc["name"] for tc in parsed])
                break

            patches = patch_call.get("arguments", {}).get("patches", [])
            logger.info("Refine iteration %d: model proposed %d patch(es)", iteration + 1, len(patches))
            for pi, p in enumerate(patches):
                logger.info("  patch[%d]: search=%r  →  replace=%r",
                             pi, (p.get("search", ""))[:100], (p.get("replace", ""))[:100])
            if not patches:
                logger.info("Refine iteration %d: empty patches list, stopping", iteration + 1)
                break

            # Apply patches
            pre_len = len(current_draft)
            current_draft, errors = _apply_patches(current_draft, patches)
            logger.info("Refine iteration %d: patches applied, draft %d→%d chars, %d error(s)",
                        iteration + 1, pre_len, len(current_draft), len(errors))
            for e in errors:
                logger.warning("Refine iteration %d patch error: %s", iteration + 1, e)

            # Re-run audit on patched draft
            report = run_audit(current_draft, phrase_bank)
            report_text = format_report(report)
            logger.info(
                "Refine iteration %d: post-audit — %d issues (cliches=%d, openers=%d, templates=%d)",
                iteration + 1, report.total_issues, report.cliche_result.flagged_count,
                len(report.monotony_result.flagged_openers), len(report.template_result.flagged_templates),
            )
            logger.info("Refine iteration %d: post-audit report:\n%s", iteration + 1, report_text)
            debug_parts.append(f"Post-iteration {iteration + 1} audit ({report.total_issues} issues):\n{report_text}")

            if report.is_clean:
                logger.info("Refine: audit clean after iteration %d", iteration + 1)
                break

            # Stall detection: if issues didn't decrease, the model can't fix what's left
            if report.total_issues >= prev_issues:
                logger.info("Refine: no progress (issues %d → %d), stopping after iteration %d",
                            prev_issues, report.total_issues, iteration + 1)
                break
            prev_issues = report.total_issues

            # Append assistant reasoning + tool result as plain assistant/user turns
            # (avoids role:tool and tool_calls which many models don't support in history)
            reasoning = resp.get("content", "") or ""
            # Summarize what the model did so it has context on the next iteration
            patch_summary = "; ".join(
                f"replaced \"{p.get('search', '')[:40]}…\"" for p in patches if p.get("search") != p.get("replace")
            ) or "no effective changes"
            assistant_recap = (reasoning + "\n\n" if reasoning else "") + f"[Applied patches: {patch_summary}]"
            msgs.append({"role": "assistant", "content": assistant_recap})

            # Feed back the updated audit report as a user turn
            tool_response_content = "\n".join(errors) + "\n\n" + report_text if errors else report_text
            msgs.append({"role": "user", "content": f"[Tool result — updated audit after your patches]\n{tool_response_content}"})
            logger.info("Refine iteration %d: appended assistant recap + user tool-result to thread (now %d turns)",
                        iteration + 1, len(msgs))

        except Exception as e:
            logger.error("Refine iteration %d failed: %s", iteration + 1, e, exc_info=True)
            debug_parts.append(f"Iteration {iteration + 1} error: {e}")
            break
    else:
        logger.warning("Refine: hit max iterations (%d) with %d issues still remaining",
                       _MAX_REFINE_ITERATIONS, report.total_issues)

    elapsed = int((time.monotonic() - t0) * 1000)
    changed = current_draft != draft
    logger.info("Refine: done in %dms, changed=%s, final_draft=%d chars", elapsed, changed, len(current_draft))

    if current_draft != draft:
        return current_draft, "\n---\n".join(debug_parts), elapsed
    return None, "\n---\n".join(debug_parts), elapsed

async def _run_pipeline(
    client: LLMClient, settings: dict, director: dict, fragments: list[dict],
    prefix: list[dict], user_message: str, phrase_bank: list[list[str]] | None = None,
) -> AsyncIterator[dict]:
    # Resolve enabled tools; disable all if agent is off
    enabled_tools = settings.get("enabled_tools") or {}
    agent_on = bool(settings.get("enable_agent", 1))
    if not agent_on:
        enabled_tools = {}

    active_moods, agent_raw, calls, latency, refined_msg, plot_direction, narration_direction, detected_repetitions = (
        director["active_moods"], "", [], 0, None, None, None, None
    )
    effective_msg = user_message
    do_refine = agent_on and enabled_tools.get("refine_apply_patch", False) and phrase_bank is not None

    # --- Agent pass: style selection + prompt rewrite ---
    if agent_on:
        yield {"event": "director_start"}
        active_moods, agent_raw, calls, latency, refined_msg, plot_direction, narration_direction, detected_repetitions = await _agent_pass(
            client, prefix, user_message, settings, director, fragments, enabled_tools
        )
        if refined_msg:
            effective_msg = refined_msg
            yield {"event": "prompt_rewritten", "data": {"refined_message": refined_msg}}

    # Build style injection block from active + newly deactivated moods
    deactivated = [f for f in fragments if f["id"] in (set(director["active_moods"]) - set(active_moods))]
    active = [f for f in fragments if f["id"] in active_moods]
    inj_block = build_style_injection(active, deactivated, plot_direction, narration_direction, detected_repetitions) if (active or deactivated or plot_direction or narration_direction or detected_repetitions) else ""

    yield {"event": "director_done", "data": {
        "active_moods": active_moods, "injection_block": inj_block, "tool_calls": calls,
        "agent_latency_ms": latency, "plot_direction": plot_direction, "narration_direction": narration_direction, "detected_repetitions": detected_repetitions,
    }}

   # --- Writer pass: stream the story response ---
    writer_tail = ""
    if inj_block:
        writer_tail += inj_block + "\n\n"
    writer_tail += effective_msg + "\n\n[OOC: Only write the continuation of the story, tool/function calling is STRICTLY FORBIDDEN now!]\n"

    writer_msgs = prefix + [{"role": "user", "content": writer_tail}]

    resp_text = ""
    async for token in _writer_pass(client, writer_msgs, settings, enabled_tools):
        resp_text += token
        yield {"event": "token", "data": token}

    # Yield base result early so caller can persist before refinement
    yield {"event": "_result", "data": {
        "active_moods": active_moods, "agent_raw": agent_raw, "calls": calls,
        "latency": latency, "refined_msg": refined_msg, "effective_msg": effective_msg,
        "resp_text": resp_text, "inj_block": inj_block, "plot_direction": plot_direction,
        "narration_direction": narration_direction, "detected_repetitions": detected_repetitions,
    }}

    # --- Refine pass: optional self-audit of the draft ---
    if do_refine and resp_text:
        logger.info("Refine pass starting (draft=%d chars, phrase_bank=%d groups)", len(resp_text), len(phrase_bank))
        try:
            refined_draft, _debug_log, _elapsed = await _refine_pass(client, prefix, effective_msg, resp_text, settings, phrase_bank or [], enabled_tools)
            if refined_draft and refined_draft != resp_text:
                resp_text = refined_draft
                yield {"event": "writer_rewrite", "data": {"refined_text": resp_text}}
                yield {"event": "_refined_result", "data": {"resp_text": resp_text}}
        except Exception as e:
            logger.error("refine pass failed, keeping original: %s", e, exc_info=True)
    elif not do_refine:
        logger.info("Refine pass skipped (do_refine=%s, resp_text=%d chars)", do_refine, len(resp_text) if resp_text else 0)


async def handle_turn(conversation_id: str, user_message: str, skip_user_persist: bool = False) -> AsyncIterator[dict]:
    try:
        settings = await db.get_settings()
        conv = await db.get_conversation(conversation_id)
        if not conv:
            yield {"event": "error", "data": "Conversation not found"}; return

        messages = await db.get_messages(conversation_id)
        director = await db.get_director_state(conversation_id)
        fragments = await db.get_fragments()
        phrase_bank = await db.get_phrase_bank()
        client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

        history, user_msg_id = messages, None
        user_parent_id = conv.get("active_leaf_id")
        next_turn = (messages[-1]["turn_index"] + 1) if messages else 0

        if skip_user_persist and messages and messages[-1]["role"] == "user":
            history, user_msg_id = messages[:-1], messages[-1]["id"]

        # Save user message BEFORE pipeline so it's preserved even if generation fails/aborts
        if not skip_user_persist:
            user_msg_id = await db.add_message(conversation_id, "user", user_message, next_turn, parent_id=user_parent_id)
            await db.set_active_leaf(conversation_id, user_msg_id)

        system_prompt, char_persona, mes_example = await _load_char_context(conv, settings)
        prefix = build_prefix(
            system_prompt, conv["character_name"], char_persona,
            conv["character_scenario"], mes_example, conv.get("post_history_instructions", ""),
            history, settings.get("user_name", "User"), settings.get("user_description", "")
        )

        res = {}
        asst_id = None
        persisted = False
        accumulated_text = ""

        try:
            async for event in _run_pipeline(client, settings, director, fragments, prefix, user_message, phrase_bank):
                if event["event"] == "token":
                    accumulated_text += event["data"]
                    yield event
                elif event["event"] == "_result":
                    res = event["data"]
                    # ── Persist assistant message after writer pass ──
                    if settings.get("enable_agent", 1):
                        await db.update_director_state(conversation_id, res["active_moods"])

                    # Update user message if agent refined it (user msg already saved before pipeline)
                    if res["refined_msg"] and user_msg_id:
                        await db.update_message_content(user_msg_id, res["effective_msg"])

                    asst_turn = next_turn + (0 if skip_user_persist else 1)
                    asst_id = await db.add_message(conversation_id, "assistant", res["resp_text"], asst_turn, parent_id=user_msg_id)

                    await db.set_active_leaf(conversation_id, asst_id)
                    await db.add_conversation_log(conversation_id, next_turn, res["agent_raw"], res["calls"], res["active_moods"], res["inj_block"], res["latency"])
                    persisted = True

                elif event["event"] == "_refined_result":
                    # ── Update assistant message in-place ──
                    res["resp_text"] = event["data"]["resp_text"]
                    if asst_id:
                        await db.update_message_content(asst_id, res["resp_text"])

                else:
                    yield event
        finally:
            # Fallback: persist assistant message if pipeline aborted before _result
            # (user message is already saved before the pipeline starts)
            # Shield DB writes from CancelledError so they complete even when
            # the async generator is being closed due to client disconnect.
            if not persisted:
                async def _fallback_persist():
                    try:
                        if res.get("active_moods") and settings.get("enable_agent", 1):
                            await db.update_director_state(conversation_id, res["active_moods"])
                        if res.get("refined_msg") and user_msg_id:
                            await db.update_message_content(user_msg_id, res["effective_msg"])
                        resp_text = res.get("resp_text", "") or accumulated_text
                        if resp_text.strip():
                            asst_turn = next_turn + (0 if skip_user_persist else 1)
                            asst_id = await db.add_message(conversation_id, "assistant", resp_text, asst_turn, parent_id=user_msg_id)
                            await db.set_active_leaf(conversation_id, asst_id)
                            logger.info("Fallback persistence saved incomplete assistant message (%d chars)", len(resp_text))
                    except Exception:
                        logger.exception("Fallback persistence failed")

                try:
                    await asyncio.shield(_fallback_persist())
                except asyncio.CancelledError:
                    # If shield itself was cancelled, retry once unshielded
                    # (the aclose() in _sse_stream shields us, but just in case)
                    try:
                        await _fallback_persist()
                    except Exception:
                        logger.exception("Fallback persistence retry failed")

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

        target = await db.get_message_by_id(assistant_msg_id)
        if not target or target["conversation_id"] != conversation_id or target["role"] != "assistant":
            yield {"event": "error", "data": "Invalid target message"}; return

        user_msg_id = target["parent_id"]
        user_msg = await db.get_message_by_id(user_msg_id) if user_msg_id else None
        if not user_msg:
            yield {"event": "error", "data": "Parent user message not found"}; return

        history = await db._get_path_to_leaf(conversation_id, user_msg.get("parent_id")) if user_msg.get("parent_id") else []
        director = await db.get_director_state(conversation_id)
        prev_moods = await db.get_moods_before_turn(conversation_id, user_msg["turn_index"])
        director = {**director, "active_moods": prev_moods}
        fragments = await db.get_fragments()
        phrase_bank = await db.get_phrase_bank()
        client = LLMClient(settings["endpoint_url"], api_key=settings.get("api_key", ""))

        system_prompt, char_persona, mes_example = await _load_char_context(conv, settings)
        prefix = build_prefix(
            system_prompt, conv["character_name"], char_persona,
            conv["character_scenario"], mes_example, conv.get("post_history_instructions", ""),
            history, settings.get("user_name", "User"), settings.get("user_description", "")
        )

        res = {}
        new_asst_id = None
        persisted = False
        accumulated_text = ""

        try:
            async for event in _run_pipeline(client, settings, director, fragments, prefix, user_msg["content"], phrase_bank):
                if event["event"] == "token":
                    accumulated_text += event["data"]
                    yield event
                elif event["event"] == "_result":
                    res = event["data"]
                    # ── Persist immediately after writer pass ──
                    if settings.get("enable_agent", 1):
                        await db.update_director_state(conversation_id, res["active_moods"])
                        if res["refined_msg"]:
                            await db.update_message_content(user_msg_id, res["refined_msg"])
                    new_asst_id = await db.add_message(conversation_id, "assistant", res["resp_text"], target["turn_index"], parent_id=user_msg_id)
                    await db.set_active_leaf(conversation_id, new_asst_id)
                    persisted = True

                elif event["event"] == "_refined_result":
                    # ── Update assistant message in-place ──
                    res["resp_text"] = event["data"]["resp_text"]
                    if new_asst_id:
                        await db.update_message_content(new_asst_id, res["resp_text"])

                else:
                    yield event
        finally:
            if not persisted:
                async def _fallback_persist_regen():
                    try:
                        if res.get("active_moods") and settings.get("enable_agent", 1):
                            await db.update_director_state(conversation_id, res["active_moods"])
                        if res.get("refined_msg") and user_msg_id:
                            await db.update_message_content(user_msg_id, res["refined_msg"])
                        resp_text = res.get("resp_text", "") or accumulated_text
                        if resp_text.strip():
                            new_asst_id = await db.add_message(conversation_id, "assistant", resp_text, target["turn_index"], parent_id=user_msg_id)
                            await db.set_active_leaf(conversation_id, new_asst_id)
                            logger.info("Fallback persistence saved incomplete assistant message (%d chars)", len(resp_text))
                    except Exception:
                        logger.exception("Fallback persistence failed")

                try:
                    await asyncio.shield(_fallback_persist_regen())
                except asyncio.CancelledError:
                    try:
                        await _fallback_persist_regen()
                    except Exception:
                        logger.exception("Fallback persistence retry failed")

        yield {"event": "done"}
    except Exception as e:
        logger.exception("Regenerate error")
        yield {"event": "error", "data": str(e)}
