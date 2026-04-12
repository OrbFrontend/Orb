from __future__ import annotations
import asyncio
import json
import logging
import re
import time
from typing import AsyncIterator, Optional

from . import database as db
from .llm_client import LLMClient, parse_tool_calls
from .audit import run_audit, format_report, AuditReport
from .slop_detector import FlaggedSentence, DetectionResult
from .opening_monotony import FlaggedOpener, MonotonyResult
from .template_repetition import FlaggedTemplate, TemplateResult

logger = logging.getLogger(__name__)


def _filter_audit_report_to_text(report: AuditReport, target_text: str) -> AuditReport:
    """Filter an audit report to only include sentences that appear in target_text.
    
    This is used when audit is run on concatenated text (including previous messages)
    but we only want to flag issues in the latest message (target_text).
    """
    target_sentences = set()
    # Simple approach: split target_text into sentences and add to set
    # Use the same sentence splitting logic as the detectors
    import re
    target_sentences_list = re.split(r'(?<=[.!?"""\'])\s+', target_text.strip())
    target_sentences = {s.strip() for s in target_sentences_list if s.strip()}
    
    # Filter cliche results
    filtered_flagged_sentences = []
    for fs in report.cliche_result.flagged_sentences:
        if fs.sentence in target_sentences:
            filtered_flagged_sentences.append(fs)
    
    filtered_cliche_result = DetectionResult(
        flagged_sentences=filtered_flagged_sentences,
        unique_cliches=report.cliche_result.unique_cliches,
        total_sentences=report.cliche_result.total_sentences,
        flagged_count=len(filtered_flagged_sentences),
    )
    
    # Filter monotony results
    filtered_flagged_openers = []
    for fo in report.monotony_result.flagged_openers:
        filtered_sentences = [s for s in fo.sentences if s in target_sentences]
        if filtered_sentences:
            # Create a new FlaggedOpener with filtered sentences
            # Adjust count and fraction based on filtered sentences
            filtered_fo = FlaggedOpener(
                opener=fo.opener,
                count=len(filtered_sentences),
                fraction=len(filtered_sentences) / report.monotony_result.total_sentences if report.monotony_result.total_sentences > 0 else 0.0,
                sentences=filtered_sentences,
            )
            filtered_flagged_openers.append(filtered_fo)
    
    filtered_monotony_result = MonotonyResult(
        flagged_openers=filtered_flagged_openers,
        all_openers=report.monotony_result.all_openers,
        total_sentences=report.monotony_result.total_sentences,
        monotony_score=report.monotony_result.monotony_score,
    )
    
    # Filter template results
    filtered_flagged_templates = []
    for ft in report.template_result.flagged_templates:
        filtered_sentences = [s for s in ft.sentences if s in target_sentences]
        if filtered_sentences:
            filtered_ft = FlaggedTemplate(
                template=ft.template,
                count=len(filtered_sentences),
                fraction=len(filtered_sentences) / report.template_result.total_sentences if report.template_result.total_sentences > 0 else 0.0,
                sentences=filtered_sentences,
            )
            filtered_flagged_templates.append(filtered_ft)
    
    filtered_template_result = TemplateResult(
        flagged_templates=filtered_flagged_templates,
        all_templates=report.template_result.all_templates,
        total_sentences=report.template_result.total_sentences,
        unique_templates=report.template_result.unique_templates,
        repetition_score=report.template_result.repetition_score,
    )
    
    return AuditReport(
        cliche_result=filtered_cliche_result,
        monotony_result=filtered_monotony_result,
        template_result=filtered_template_result,
    )


def replace_placeholders(text: str, user_name: str, char_name: str) -> str:
    """Replace {{user}} and {{char}} placeholders with actual names."""
    if not text or not isinstance(text, str):
        return text or ''
    result = text
    if user_name:
        result = re.sub(r'\{\{user\}\}', user_name, result, flags=re.IGNORECASE)
    if char_name:
        result = re.sub(r'\{\{char\}\}', char_name, result, flags=re.IGNORECASE)
    return result


# --- Agent tool definitions (OpenAI function-calling format) ---

AGENT_TOOLS = [{
    "type": "function",
    "function": {
        "name": "direct_scene",
        "description": "Call this to direct the scene. Deduce what the user wants to see and show them. Combine and configure the moods, extract keywords, summarize the plot, specify the direction the scene should take, detect and report repetitive tropes, phrases, subjects, and narrative patterns to avoid. Be very specific with the directions.",
        "parameters": {
            "type": "object",
            "properties": {
                "moods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of moods to activate.",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of nouns (keywords) to remind the important subjects in the roleplay so far. This list shouldn't grow too long (keep under 6 items). Extract from the messages and plot summary. Ignore obvious things like names of the characters. Examples: 'ancient Egypt', 'headlock', 'monetary deal', 'language/accent', 'desert night', 'six-sided dice', 'discarded belt'. Avoid generic concepts (e.g. 'anger', 'ruin', etc.)",
                },
                "plot_summary": {
                    "type": "string",
                    "description": "A brief and specific summary of what has happened so far in the story. Call things for what they are, avoid being generic, avoid adjectives. 3 sentences max (e.g. Rob was working on his lake house when his wife called for him to help moving some furnitue. The weather was hot so he took off his shirt. Then the couch fell on his leg, eliciting his pain receptors.).",
                },
                "plot_direction": {
                    "type": "string",
                    "description": "What happens next in the story — events, actions, reveals, turns of fate (e.g. 'she continues to bear down in a squatting position', 'the attack tears off a piece of her clothing', 'Jack can tell she's lying and calls her out it because they have been friends forever', 'she pretends not to know what Vodka is to keep up the innocent act'). Keep to one short sentence.",
                },
                "writing_direction": {
                    "type": "string",
                    "description": "How the scene should be written — focus, emphasis, descriptive lens, internal state (e.g. 'focus on his anxious tics in detail', 'narrate her spiraling thoughts on why it went wrong', 'describe her exposed stomach vividly', 'describe what he sees in the picture', 'emphasize her speech quirks'). Keep to one short sentence. Show don't tell.",
                },
                "detected_repetitions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Specific tropes, phrases, subjects, or narrative patterns that are recently overused in the narration. Only report the ones that are recent (e.g. 'repeated description of eyes', 'mundane narration of internal struggles', 'overuse of murderous rage', 'mentions of jaw tightening', 'repeated trope of the user getting away with everything', 'constant narration of his accent without showing it').",
                },
            },
            "required": ["moods", "keywords", "plot_summary", "plot_direction", "writing_direction"],
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

MINIMIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "minimize",
        "description": "Replace the entire draft with a shorter, more concise rewrite that respects the maximum paragraph count. Preserve the author's voice, vocabulary, all key story beats, and any special formatting or code.",
        "parameters": {
            "type": "object",
            "properties": {
                "rewritten_text": {
                    "type": "string",
                    "description": "The condensed rewrite of the entire draft. Must be within the required paragraph limit.",
                },
            },
            "required": ["rewritten_text"],
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
    "- For banned phrases: rewrite the sentence to remove the banned phrase entirely. Note: The audit report may show the canonical phrase name (e.g., 'ozone'), but you need to remove the actual variant that appears in the sentence (e.g., 'electric').\n"
    "- For repetitive openers: change how the sentence begins.\n"
    "- For repetitive templates: restructure the sentence (reorder clauses, combine, vary syntax)."
    "- If request makes no sense then just skip because sometimes the report has issues due do its probabilistic nature."
)

LENGTH_GUARD_INSTRUCTIONS = (
    "LENGTH GUARD: The draft is too long ({word_count} words). "
    "Rewrite the entire response using the `minimize` tool so it is at most {max_paragraphs} paragraphs. "
    "Preserve the author's voice, word choices, and all key story beats. Be more concise."
)

_MAX_REFINE_ITERATIONS = 3

TOOLS: dict[str, dict] = {
    "direct_scene": {"choice": {"type": "function", "function": {"name": "direct_scene"}}, "schema": AGENT_TOOLS[0], "reasoning_enabled": True},
    "rewrite_user_prompt": {"choice": {"type": "function", "function": {"name": "rewrite_user_prompt"}}, "schema": REWRITE_PROMPT_TOOL, "reasoning_enabled": True},
    "refine_apply_patch": {"choice": {"type": "function", "function": {"name": "refine_apply_patch"}}, "schema": REFINE_APPLY_PATCH_TOOL, "reasoning_enabled": False},
    "minimize": {"choice": {"type": "function", "function": {"name": "minimize"}}, "schema": MINIMIZE_TOOL, "reasoning_enabled": True},
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
        "[OOC] Let's pause to improve the roleplay. Use tool calls to accomplish your task accurately and creatively. Your output will immediately affect how the scenario plays out. Be decisive and avoid overthinking. Think outside the box.",
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
    plot_direction: str | None = None, writing_direction: str | None = None,
    detected_repetitions: list[str] | None = None,
    plot_summary: str | None = None,
    keywords: list[str] | None = None,
) -> str:
    parts = ["<current_scene_direction>"]
    if plot_summary:
        parts.append(f"  <plot_summary>{plot_summary}</plot_summary>")
    if plot_direction:
        parts.append(f"  <plot>{plot_direction}</plot>")
    if writing_direction:
        parts.append(f"  <narration>{writing_direction}</narration>")
    if detected_repetitions:
        parts.append("  <avoid>")
        for phrase in detected_repetitions:
            parts.append(f"    - {phrase}")
        parts.append("  </avoid>")
    if keywords:
        parts.append("  <keywords>")
        for kw in keywords:
            parts.append(f"    - {kw}")
        parts.append("  </keywords>")
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
    # Replace placeholders in character card fields
    resolved_persona = replace_placeholders(char_persona, user_name, char_name)
    resolved_scenario = replace_placeholders(char_scenario, user_name, char_name)
    resolved_mes_example = replace_placeholders(mes_example, user_name, char_name)
    resolved_post_history = replace_placeholders(post_history_instructions, user_name, char_name)
    resolved_user_description = replace_placeholders(user_description, user_name, char_name)
    
    parts = [system_prompt]
    if char_name: parts.append(f"\n\n## Character: {char_name}")
    if resolved_persona: parts.append(f"\n{resolved_persona}")
    if resolved_scenario: parts.append(f"\n\n## Scenario\n{resolved_scenario}")
    if resolved_mes_example: parts.append(f"\n\n## Example Dialogue\n{resolved_mes_example}")
    if resolved_post_history: parts.append(f"\n\n## Additional Instructions\n{resolved_post_history}")
    if resolved_user_description: parts.append(f"\n\n## User: {user_name or 'User'}\n{resolved_user_description}")
    
    # Process messages: replace placeholders in each message content
    processed_messages = []
    for m in (messages or []):
        resolved_content = replace_placeholders(m["content"], user_name, char_name)
        processed_messages.append({"role": m["role"], "content": resolved_content})
    
    return [{"role": "system", "content": "".join(parts)}] + processed_messages


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
    logger.info("Writer pass: tools included=%s", json.dumps([s["function"]["name"] for s in schemas]) if schemas else "[]")
    extra = {"tools": schemas, "tool_choice": "none"} if schemas else {}
    async for token in client.stream(messages=msgs, model=settings["model_name"], **extra, **params):
        yield token


async def _agent_pass(
    client: LLMClient, prefix: list[dict], user_message: str, settings: dict,
    director: dict, fragments: list[dict], enabled_tools: dict | None = None
) -> tuple[list[str], str, list, int, str | None, str | None, str | None, list[str] | None, str | None, list[str] | None]:
    active_moods, refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords, all_calls, last_raw = director["active_moods"], None, None, None, None, None, director.get("keywords", []), [], ""
    tool_names = ["direct_scene"] if enabled_tools is None else [
        n for n, on in enabled_tools.items() if on and n in TOOLS and n not in POST_WRITER_TOOLS
    ]
    if not tool_names:
        return active_moods, "", [], 0, None, None, None, None, None, None

    tool_schemas = _enabled_schemas(enabled_tools)
    logger.info("Director pass: tools included=%s", json.dumps([s["function"]["name"] for s in tool_schemas]) if tool_schemas else "[]")

    t0 = time.monotonic()
    for name in tool_names:
        msgs = prefix + [{"role": "user", "content": build_tool_prompt(name, user_message, active_moods, fragments)}]
        logger.info("Agent tool=%s prompt:\n%s", name, json.dumps(msgs, indent=2, ensure_ascii=False))
        try:
            # Check if reasoning should be enabled for this tool
            reasoning_config = None
            tool_config = TOOLS.get(name, {})
            if not tool_config.get("reasoning_enabled", True):
                reasoning_config = {"enabled": False}
            
            resp = await client.complete(
                messages=msgs, model=settings["model_name"], tools=tool_schemas,
                tool_choice=TOOLS[name]["choice"], temperature=0.25, max_tokens=8192,
                **({"reasoning": reasoning_config} if reasoning_config else {}),
            )
            last_raw = json.dumps(resp, default=str)
            logger.info("Agent tool=%s output:\n%s", name, last_raw)
            if parsed := parse_tool_calls(resp):
                all_calls.extend(parsed)
                active_moods, new_refined, new_plot, new_narration, new_detected_repetitions, new_plot_summary, new_keywords = apply_tool_calls(parsed, active_moods)
                if new_refined:
                    refined_msg = new_refined
                if new_plot:
                    plot_direction = new_plot
                if new_narration:
                    writing_direction = new_narration
                if new_detected_repetitions:
                    detected_repetitions = new_detected_repetitions
                if new_plot_summary:
                    plot_summary = new_plot_summary
                if new_keywords:
                    # Limit keywords to a few items only
                    keywords = new_keywords[:6]
            else:
                logger.info("Agent tool=%s: model skipped", name)
        except Exception as e:
            logger.error("Agent tool=%s failed: %s", name, e)
            last_raw = f"ERROR: {e}"

    return active_moods, last_raw, all_calls, int((time.monotonic() - t0) * 1000), refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords


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
            err = f"Error: Patch {i} is a no-op (search === replace). You must provide different replacement text."
            logger.info("Patch %d: no-op (search === replace), error: %s", i, err)
            errors.append(err)
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
    audit_enabled: bool = True,
    length_guard: dict | None = None,
) -> tuple[str | None, str, int]:
    """ReAct-style refinement loop with optional audit and/or length guard.

    Each sub-pass (Output Auditor, Length Guard, …) is independently gated
    by its own flag so they can be enabled/disabled in any combination.

    Returns (refined_draft_or_None, debug_log, latency_ms).
    """
    t0 = time.monotonic()
    debug_parts: list[str] = []

    # ── Output Auditor sub-pass ──────────────────────────────────────────
    if audit_enabled:
        # Extract last 3 assistant messages from prefix for context
        # Note: Length guard only scans the latest message (draft), but repetition
        # detection should consider patterns across the last 3 assistant messages.
        assistant_messages = []
        for msg in reversed(prefix):
            if msg.get("role") == "assistant":
                assistant_messages.append(msg.get("content", ""))
                if len(assistant_messages) >= 2:  # We want up to 2 previous messages (plus current draft makes 3)
                    break
        
        # Include current draft as the latest message
        text_for_audit = draft
        if assistant_messages:
            # Concatenate previous assistant messages (oldest to newest) with current draft
            # This allows detection of repetitions across multiple responses
            context_text = "\n\n".join(reversed(assistant_messages))
            text_for_audit = context_text + "\n\n" + draft
        
        logger.info("Refine: running audit on draft (%d chars) with %d previous assistant messages for context, phrase_bank has %d groups",
                   len(draft), len(assistant_messages), len(phrase_bank))
        if assistant_messages:
            logger.info("Refine: previous assistant messages (oldest to newest):")
            for i, msg in enumerate(reversed(assistant_messages)):
                logger.info("  [%d/%d] %d chars: %.100s%s",
                           i+1, len(assistant_messages), len(msg),
                           msg, "..." if len(msg) > 100 else "")
        logger.info("Refine: text being audited (context + draft) = %d chars total", len(text_for_audit))
        logger.debug("Refine: full text for audit:\n%s", text_for_audit)
        report = run_audit(text_for_audit, phrase_bank)
        
        # Filter report to only include issues in the current draft (not previous messages)
        filtered_report = _filter_audit_report_to_text(report, draft)
        
        report_text = format_report(filtered_report)
        logger.info(
            "Refine: initial audit — %d issues (cliches=%d, openers=%d, templates=%d) after filtering to current draft",
            filtered_report.total_issues, filtered_report.cliche_result.flagged_count,
            len(filtered_report.monotony_result.flagged_openers), len(filtered_report.template_result.flagged_templates),
        )
        logger.info("Refine: initial audit report:\n%s", report_text)
        debug_parts.append(f"Initial audit ({filtered_report.total_issues} issues):\n{report_text}")
        
        # Use filtered report for the rest of the refinement pass
        report = filtered_report
    else:
        report = AuditReport.clean()
        report_text = ""
        logger.info("Refine: audit disabled, skipping scanners")

    # ── Length Guard sub-pass ────────────────────────────────────────────
    length_guard_triggered = False
    length_guard_instruction = ""
    refine_tools: list[dict] = []
    if audit_enabled:
        refine_tools.append(REFINE_APPLY_PATCH_TOOL)
    if length_guard and length_guard.get("enabled"):
        word_count = len(draft.split())
        max_words = length_guard.get("max_words", 280)
        max_paragraphs = length_guard.get("max_paragraphs", 3)
        if word_count > max_words:
            length_guard_triggered = True
            length_guard_instruction = LENGTH_GUARD_INSTRUCTIONS.format(
                word_count=word_count, max_paragraphs=max_paragraphs
            )
            refine_tools.append(MINIMIZE_TOOL)
            logger.info("Refine: length guard triggered (word_count=%d > max_words=%d, max_paragraphs=%d)",
                        word_count, max_words, max_paragraphs)
            debug_parts.append(f"Length guard triggered: {word_count} words (max {max_words}), target {max_paragraphs} paragraphs")

    if report.is_clean and not length_guard_triggered:
        logger.info("Refine: audit clean and no length guard, skipping LLM loop")
        return None, "\n---\n".join(debug_parts), int((time.monotonic() - t0) * 1000)

    if not refine_tools:
        logger.info("Refine: no refine tools applicable, skipping LLM loop")
        return None, "\n---\n".join(debug_parts), int((time.monotonic() - t0) * 1000)

    # Step 2: Build message context (reuses KV cache prefix)
    # Use "user" role for the refine instruction — system role after assistant turns is
    # rejected by OpenAI-compatible APIs (e.g. OpenRouter returns 400).
    refine_instruction_parts = []
    if audit_enabled and not report.is_clean:
        refine_instruction_parts.append(REFINE_AGENT_INSTRUCTIONS)
        refine_instruction_parts.append(report_text)
    if length_guard_triggered:
        refine_instruction_parts.append(length_guard_instruction)
    msgs = prefix + [
        {"role": "user", "content": effective_msg},
        {"role": "assistant", "content": draft},
        {"role": "user", "content": "\n\n".join(refine_instruction_parts)},
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
            logger.info("Refine iteration %d: calling LLM (model=%s, max_tokens=4096, temp=0.25), tools included=%s",
                         iteration + 1, settings["model_name"],
                         json.dumps([t["function"]["name"] for t in refine_tools]))
            try:
                # Determine if we should disable reasoning based on tool configuration
                reasoning_config = None
                # Check all tools in refine_tools for reasoning_enabled=False
                # If any tool has reasoning disabled, disable reasoning for the LLM call
                for tool_schema in refine_tools:
                    tool_name = tool_schema["function"]["name"]
                    tool_config = TOOLS.get(tool_name, {})
                    if not tool_config.get("reasoning_enabled", True):
                        reasoning_config = {"enabled": False}
                        logger.info("Refine iteration %d: disabling reasoning for tool=%s",
                                   iteration + 1, tool_name)
                        break
                
                resp = await client.complete(
                    messages=msgs,
                    model=settings["model_name"],
                    tools=refine_tools,
                    tool_choice=(
                        {"type": "function", "function": {"name": "minimize"}}
                        if (length_guard_triggered and report.is_clean)
                        else ("auto" if length_guard_triggered
                              else (TOOLS["refine_apply_patch"]["choice"] if audit_enabled else "auto"))
                    ),
                    temperature=0.25,
                    max_tokens=8192,
                    **({"reasoning": reasoning_config} if reasoning_config else {}),
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

            # Handle minimize tool call (full rewrite for length guard)
            minimize_call = next((tc for tc in parsed if tc["name"] == "minimize"), None)
            if minimize_call:
                rewritten = minimize_call.get("arguments", {}).get("rewritten_text", "").strip()
                if rewritten:
                    pre_len = len(current_draft)
                    current_draft = rewritten
                    length_guard_triggered = False  # satisfied after minimize
                    logger.info("Refine iteration %d: minimize applied, draft %d→%d chars",
                                iteration + 1, pre_len, len(current_draft))
                    debug_parts.append(f"Iteration {iteration + 1}: minimize applied ({pre_len}→{len(current_draft)} chars)")
                else:
                    logger.info("Refine iteration %d: minimize call had empty rewritten_text, stopping", iteration + 1)
                    break
                # Re-run audit after minimize (only if audit is enabled)
                if audit_enabled:
                    # Use same context as initial audit (previous assistant messages)
                    text_for_audit = current_draft
                    if assistant_messages:
                        context_text = "\n\n".join(reversed(assistant_messages))
                        text_for_audit = context_text + "\n\n" + current_draft
                    logger.info("Refine iteration %d: post-minimize audit with %d previous assistant messages, text length=%d",
                               iteration + 1, len(assistant_messages) if assistant_messages else 0, len(text_for_audit))
                    report = run_audit(text_for_audit, phrase_bank)
                    # Filter to only include issues in current draft
                    report = _filter_audit_report_to_text(report, current_draft)
                    report_text = format_report(report)
                    debug_parts.append(f"Post-minimize audit ({report.total_issues} issues):\n{report_text}")
                else:
                    report = AuditReport.clean()
                    report_text = ""
                if report.is_clean:
                    break
                # Continue to patch remaining issues in next iteration
                refine_tools = [REFINE_APPLY_PATCH_TOOL] if audit_enabled else []
                prev_issues = report.total_issues
                # Update both the assistant draft and the refine instruction so the
                # patch model sees the minimized text, not the original.
                msgs[-2] = {"role": "assistant", "content": current_draft}
                msgs[-1] = {"role": "user", "content": (REFINE_AGENT_INSTRUCTIONS + "\n\n" + report_text) if audit_enabled else length_guard_instruction}
                continue

            # Find refine_apply_patch call
            patch_call = next((tc for tc in parsed if tc["name"] == "refine_apply_patch"), None)
            if not patch_call:
                logger.info("Refine iteration %d: no recognized tool call in %s, stopping",
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

            # Re-run audit on patched draft (with context from previous assistant messages)
            text_for_audit = current_draft
            if assistant_messages:
                context_text = "\n\n".join(reversed(assistant_messages))
                text_for_audit = context_text + "\n\n" + current_draft
            logger.info("Refine iteration %d: post-patch audit with %d previous assistant messages, text length=%d",
                       iteration + 1, len(assistant_messages) if assistant_messages else 0, len(text_for_audit))
            report = run_audit(text_for_audit, phrase_bank)
            # Filter to only include issues in current draft
            report = _filter_audit_report_to_text(report, current_draft)
            report_text = format_report(report)
            logger.info(
                "Refine iteration %d: post-audit — %d issues (cliches=%d, openers=%d, templates=%d)",
                iteration + 1, report.total_issues, report.cliche_result.flagged_count,
                len(report.monotony_result.flagged_openers), len(report.template_result.flagged_templates),
            )
            logger.info("Refine iteration %d: post-audit report:\n%s", iteration + 1, report_text)
            debug_parts.append(f"Post-iteration {iteration + 1} audit ({report.total_issues} issues):\n{report_text}")

            if report.is_clean:
                if not length_guard_triggered:
                    logger.info("Refine: audit clean after iteration %d", iteration + 1)
                    break
                # Audit is clean but length guard still pending — switch to minimize-only
                # for the next iteration rather than breaking.
                logger.info("Refine: audit clean after iteration %d, length guard still pending — queuing minimize",
                            iteration + 1)
                refine_tools = [MINIMIZE_TOOL]
                # No need for patch tool anymore since audit is clean

            # Stall detection: if issues didn't decrease, the model can't fix what's left
            if report.total_issues >= prev_issues:
                logger.info("Refine: no progress (issues %d → %d), stopping after iteration %d",
                            prev_issues, report.total_issues, iteration + 1)
                break
            prev_issues = report.total_issues

            # Append assistant reasoning + tool result as plain assistant/user turns
            # (avoids role:tool and tool_calls which many models don't support in history)
            reasoning = resp.get("content", "") or ""
            reasoning_content = resp.get("reasoning_content", "") or ""
            # Check if reasoning should be included based on tool configuration
            tool_name = "refine_apply_patch"  # This is the only tool that reaches this point in refine pass
            reasoning_enabled = TOOLS.get(tool_name, {}).get("reasoning_enabled", True)
            
            logger.info("Refine iteration %d: reasoning fields - content=%d chars, reasoning_content=%d chars, reasoning_enabled=%s",
                        iteration + 1, len(reasoning), len(reasoning_content), reasoning_enabled)
            
            # Summarize what the model did so it has context on the next iteration
            patch_summary = "; ".join(
                f"replaced \"{p.get('search', '')[:40]}…\"" for p in patches if p.get("search") != p.get("replace")
            ) or "no effective changes"
            
            # Only include reasoning if enabled for this tool
            # Note: Some models return reasoning in 'reasoning_content' field instead of 'content'
            if reasoning_enabled and (reasoning or reasoning_content):
                combined_reasoning = (reasoning + "\n" + reasoning_content).strip()
                assistant_recap = combined_reasoning + "\n\n" + f"[Applied patches: {patch_summary}]"
            else:
                assistant_recap = f"[Applied patches: {patch_summary}]"
                
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

    active_moods, agent_raw, calls, latency, refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary = (
        director["active_moods"], "", [], 0, None, None, None, None, None
    )
    effective_msg = user_message
    audit_enabled = agent_on and bool(enabled_tools.get("refine_apply_patch", False)) and phrase_bank is not None

    # Length guard settings
    length_guard_enabled = bool(settings.get("length_guard_enabled", False))
    length_guard = {
        "enabled": length_guard_enabled,
        "max_words": int(settings.get("length_guard_max_words", 240)),
        "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
    } if length_guard_enabled else None

    # Refine pass runs if *any* refine sub-pass is active
    do_refine = audit_enabled or (length_guard_enabled and agent_on)

    # --- Agent pass: style selection + prompt rewrite ---
    # Only run if agent is on AND at least one pre-writer tool is enabled
    has_pre_writer_tools = any(enabled_tools.get(n, False) for n in TOOLS if n not in POST_WRITER_TOOLS)
    if agent_on and has_pre_writer_tools:
        yield {"event": "director_start"}
        active_moods, agent_raw, calls, latency, refined_msg, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords = await _agent_pass(
            client, prefix, user_message, settings, director, fragments, enabled_tools
        )
        if refined_msg:
            effective_msg = refined_msg
            yield {"event": "prompt_rewritten", "data": {"refined_message": refined_msg}}

    # Build style injection block from active + newly deactivated moods
    deactivated = [f for f in fragments if f["id"] in (set(director["active_moods"]) - set(active_moods))]
    active = [f for f in fragments if f["id"] in active_moods]
    inj_block = build_style_injection(active, deactivated, plot_direction, writing_direction, detected_repetitions, plot_summary, keywords) if (active or deactivated or plot_direction or writing_direction or detected_repetitions or plot_summary or keywords) else ""

    yield {"event": "director_done", "data": {
        "active_moods": active_moods, "injection_block": inj_block, "tool_calls": calls,
        "agent_latency_ms": latency, "plot_direction": plot_direction, "writing_direction": writing_direction, "detected_repetitions": detected_repetitions, "plot_summary": plot_summary, "keywords": keywords,
    }}

    # --- Writer pass: stream the story response ---
    writer_tail = ""
    if inj_block:
        writer_tail += inj_block + "\n\n"
    if length_guard:
        writer_tail += f"[Length constraint: {length_guard['max_paragraphs']} paragraphs max, {length_guard['max_words']} words or fewer]\n"
    writer_tail += "[OOC: Only write the continuation of the story, tool/function calling is STRICTLY FORBIDDEN now!]\n\n" + effective_msg + "\n\n"

    # Inject length constraint into system message for writer pass
    # writer_prefix = prefix
    # if length_guard and prefix and prefix[0]["role"] == "system":
    #     lg_sys = f"\n\n[Length constraint: Write at most {length_guard['max_paragraphs']} paragraphs, {length_guard['max_words']} words or fewer per response.]"
    #     writer_prefix = [{"role": "system", "content": prefix[0]["content"] + lg_sys}] + list(prefix[1:])

    # writer_msgs = writer_prefix + [{"role": "user", "content": writer_tail}]

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
        "writing_direction": writing_direction, "detected_repetitions": detected_repetitions, "plot_summary": plot_summary,
        "keywords": keywords,
    }}

    # --- Refine pass: optional self-audit of the draft ---
    if do_refine and resp_text:
        logger.info("Refine pass starting (draft=%d chars, phrase_bank=%d groups)", len(resp_text), len(phrase_bank) if phrase_bank else 0)
        try:
            refined_draft, _debug_log, _elapsed = await _refine_pass(client, prefix, effective_msg, resp_text, settings, phrase_bank or [], audit_enabled, length_guard)
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
        # Filter out disabled fragments
        fragments = [f for f in fragments if f.get("enabled", True)]
        # Also remove disabled fragments from active moods
        if director and director.get("active_moods"):
            enabled_ids = {f["id"] for f in fragments}
            director["active_moods"] = [mood for mood in director["active_moods"] if mood in enabled_ids]
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
                        await db.update_director_state(conversation_id, res["active_moods"], res.get("keywords"))

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
                            await db.update_director_state(conversation_id, res["active_moods"], res.get("keywords"))
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
        fragments = await db.get_fragments()
        # Filter out disabled fragments
        fragments = [f for f in fragments if f.get("enabled", True)]
        # Also remove disabled fragments from active moods
        if director and director.get("active_moods"):
            enabled_ids = {f["id"] for f in fragments}
            director["active_moods"] = [mood for mood in director["active_moods"] if mood in enabled_ids]
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
                        await db.update_director_state(conversation_id, res["active_moods"], res.get("keywords"))
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
