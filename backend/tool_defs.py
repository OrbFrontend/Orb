"""
tool_defs.py — Tool schemas, constants, and helper lookups for the orchestrator pipeline.
"""
from __future__ import annotations


# ── Agent tool definitions (OpenAI function-calling format)

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
                    "description": "Specific tropes, phrases, subjects, or narrative patterns that are recently overused in the narration (e.g. 'repeated description of eyes', 'mundane narration of internal struggles', 'overuse of murderous rage', 'mentions of jaw tightening', 'repeated trope of the user getting away with everything', 'constant narration of his accent without showing it'). Maybe have up 8 items.",
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

REFINE_REWRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "refine_rewrite",
        "description": "Replace the entire draft with a refined rewrite. Use when length guard is triggered or when audit issues require a complete rewrite. Preserve all key story beats, the author's voice, and any special formatting or code.",
        "parameters": {
            "type": "object",
            "properties": {
                "rewritten_text": {
                    "type": "string",
                    "description": "The refined rewrite of the entire draft. Should address length constraints and/or audit issues while preserving the original intent.",
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


# ── Instruction templates

# Always included — tells the model who it is and what the assistant
# message above represents.  Without this, the model sees the roleplay
# system prompt plus a bare instruction and wastes tokens reasoning
# about context.
REFINE_PREAMBLE = (
    "You are the Refinement Agent. The assistant message above is a draft "
    "that needs improvement based on the issues described below. Use the "
    "provided tools to apply the required changes. Call exactly ONE tool."
)

# Sent only when the audit flagged issues (banned phrases, repetitive
# openers/templates).  Contains tool-selection logic, patching rules,
# and rewriting rules.
REFINE_AUDIT_INSTRUCTIONS = (
    "TOOL SELECTION RULES:\n"
    "1. If there are AUDIT ISSUES (banned phrases, repetitive openers, or repetitive templates) AND NO LENGTH GUARD: Use `refine_apply_patch` to fix each issue with surgical patches.\n"
    "2. If there is a LENGTH GUARD (draft too long) AND NO AUDIT ISSUES: Use `refine_rewrite` to produce a concise rewrite within the specified limits.\n"
    "3. If there are BOTH AUDIT ISSUES AND LENGTH GUARD: Use `refine_rewrite` to address both concerns in a single comprehensive rewrite.\n\n"
    "PATCHING RULES (when using `refine_apply_patch`):\n"
    "- Send ONE `refine_apply_patch` call with one patch per flagged issue.\n"
    "- The `search` field must be copied EXACTLY from the draft text above — including all punctuation and quotes.\n"
    "- Each patch must target a DIFFERENT, non-overlapping piece of text.\n"
    "- Do NOT send a patch where `search` and `replace` are identical.\n"
    "- Keep replacements close in length to the original. Preserve the author's voice.\n"
    "- For banned phrases: rewrite the sentence to remove the banned phrase entirely. Note: The audit report may show the canonical phrase name (e.g., 'ozone'), but you need to remove the actual variant that appears in the sentence (e.g., 'electric').\n"
    "- For repetitive openers: change how the sentence begins.\n"
    "- For repetitive templates: restructure the sentence (reorder clauses, combine, vary syntax).\n\n"
    "REWRITING RULES (when using `refine_rewrite`):\n"
    "- Send ONE `refine_rewrite` call with the complete rewritten text.\n"
    "- Address all audit issues (if any) while also respecting length constraints.\n"
    "- Preserve the author's word choices, and all key story beats.\n"
    "- First priority is to get rid of repetitiveness.\n"
    "- Be more concise but maintain coherence and narrative flow.\n\n"
    "GENERAL NOTES:\n"
    "- If the audit report seems incorrect or makes no sense, you may skip fixing those specific issues.\n"
    "- Always choose the most appropriate tool based on the combination of issues presented."
)

# Keep for backward compat — full instructions = preamble + audit rules.
REFINE_AGENT_INSTRUCTIONS = REFINE_PREAMBLE + "\n\n" + REFINE_AUDIT_INSTRUCTIONS

LENGTH_GUARD_INSTRUCTIONS = (
    "LENGTH GUARD: The draft is {word_count} words — too long. "
    "Call `refine_rewrite` with a condensed version: at most {max_paragraphs} paragraphs "
    "and {max_words} words. Preserve the author's voice and all key story beats."
)

MAX_REFINE_ITERATIONS = 3


# ── Tool registry & helpers

TOOLS: dict[str, dict] = {
    "direct_scene": {"choice": {"type": "function", "function": {"name": "direct_scene"}}, "schema": AGENT_TOOLS[0], "reasoning_enabled": True},
    "rewrite_user_prompt": {"choice": {"type": "function", "function": {"name": "rewrite_user_prompt"}}, "schema": REWRITE_PROMPT_TOOL, "reasoning_enabled": True},
    "refine_apply_patch": {"choice": {"type": "function", "function": {"name": "refine_apply_patch"}}, "schema": REFINE_APPLY_PATCH_TOOL, "reasoning_enabled": False},
    "refine_rewrite": {"choice": {"type": "function", "function": {"name": "refine_rewrite"}}, "schema": REFINE_REWRITE_TOOL, "reasoning_enabled": False},
}

POST_WRITER_TOOLS = {"refine_apply_patch"}
ALL_SCHEMAS = [t["schema"] for t in TOOLS.values()]


def enabled_schemas(enabled_tools: dict | None) -> list[dict]:
    """Return tool schemas. None means 'all enabled', {} means 'all disabled'."""
    if enabled_tools is None:
        return ALL_SCHEMAS
    return [TOOLS[n]["schema"] for n in TOOLS if enabled_tools.get(n, False)]


def reasoning_config_for_tool(tool_name: str) -> dict | None:
    """Return {"enabled": False} if reasoning is disabled for *tool_name*, else None."""
    cfg = TOOLS.get(tool_name, {})
    if not cfg.get("reasoning_enabled", True):
        return {"enabled": False}
    return None


def reasoning_config_for_schemas(schemas: list[dict]) -> dict | None:
    """If *any* schema in the list has reasoning disabled, return {"enabled": False}."""
    for schema in schemas:
        name = schema["function"]["name"]
        rc = reasoning_config_for_tool(name)
        if rc is not None:
            return rc
    return None