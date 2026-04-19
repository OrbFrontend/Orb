"""
tool_defs.py — Tool schemas, constants, and helper lookups for the orchestrator pipeline.
"""

from __future__ import annotations


# ── Agent tool definitions (OpenAI function-calling format)

AGENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "direct_scene",
            "description": "Call this to direct the scene. Deduce what the user wants to see and show them. Combine and configure the moods; extract keywords; summarize the plot; specify the direction the scene should take; detect and report repetitive tropes, phrases, subjects, plot points, narrative patterns to avoid. Be very specific and intentional with the directions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plot_summary": {
                        "type": "string",
                        "description": "A brief and specific summary of what has happened so far in the story. Call things for what they are, avoid being generic, avoid adjectives. 3 sentences max (e.g. Rob was working on his lake house when his wife called for him to help moving some furnitue. The weather was hot so he took off his shirt. Then the couch fell on his leg, eliciting his pain receptors.).",
                    },
                    "user_intent": {
                        "type": "string",
                        "description": "Hidden/subtle intention of the user based on their input - what they want to see. Be extremely literal and specific (e.g. 'This crosses the line, the user wants to find out what happens when the boundaries are crossed', 'The user clearly wants his friend to get mad and fight back', 'The user is confessing his love in a roundabout way', 'The user wants to push the scenario forward already').",
                    },
                    "keywords": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of nouns (keywords) to remind the important subjects in the roleplay so far. This list shouldn't grow too long (keep under 6 items). Extract from the messages and plot summary. Ignore obvious things like names of the characters. Examples: 'ancient Egypt', 'headlock', 'monetary deal', 'language/accent', 'desert night', 'six-sided dice', 'discarded belt'. Avoid generic concepts (e.g. 'anger', 'ruin', etc.)",
                    },
                    "next_event": {
                        "type": "string",
                        "description": "What happens immediately next in the story — the next event, action, reveal, or turn of fate (e.g. 'This act crosses personal boundaries. The character snaps and fights back.', 'The attack tears off a chunk of her clothing. She frantically tries to cover herself', 'Jack can tell she's lying. He calls her out on it because they have been friends forever', 'She pretends not to know what Vodka is to keep up the innocent act', 'He gets bored and shifts focus to something else entirely'). Keep to two short sentences.",
                    },
                    "moods": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of moods to activate. Leave empty for a neutral tone.",
                    },
                    "writing_direction": {
                        "type": "string",
                        "description": "How the scene should be written — focus, emphasis, descriptive lens, internal state (e.g. 'focus on his anxious tics in detail', 'narrate her spiraling thoughts on why it went wrong', 'describe her exposed stomach vividly', 'describe what he sees in the picture', 'emphasize her speech quirks'). Keep to one short sentence. Show don't tell.",
                    },
                    "detected_repetitions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Specific tropes, phrases, subjects, plot points, narrative patterns that are recently overused in the narration (e.g. 'banal description of eyes', 'mundane narration of internal struggles', 'overuse of murderous rage', 'repeated trope of the user getting away with everything', 'constant narration of his accent without showing it', 'constant focus on the tree'). This list may have up to 8 items.",
                    },
                },
                "required": [
                    "keywords",
                    "plot_summary",
                    "next_event",
                    "writing_direction",
                ],
            },
        },
    }
]

REWRITE_PROMPT_TOOL = {
    "type": "function",
    "function": {
        "name": "rewrite_user_prompt",
        "description": 'Rewrite the user\'s message into a more detailed action or dialogue. Use ONLY when the input is too short or vague (e.g. "I laugh.", "Sure, what is it?", "I nod.") to generate a compelling response. Write 2 sentences max, be succinct. If the message is already detailed enough, leave empty. Do NOT call direct_scene even if it is available!',
        "parameters": {
            "type": "object",
            "properties": {
                "refined_message": {
                    "type": "string",
                    "description": "An improved, more detailed version of the user's message, keep the same perspective. Leave empty or omit if already rich enough.",
                },
            },
            "required": [],
        },
    },
}

EDITOR_REWRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "editor_rewrite",
        "description": "Replace the entire draft with a refined rewrite. Use when length guard is triggered or when audit issues require a complete rewrite. Preserve all key story beats, the author's vocabulary, and any special formatting or code.",
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

EDITOR_APPLY_PATCH_TOOL = {
    "type": "function",
    "function": {
        "name": "editor_apply_patch",
        "description": "Apply one or more exact text replacements to the draft. Each 'search' must exactly match current draft text (case-sensitive, including punctuation). Returns an updated Audit Report.",
        "parameters": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "search": {
                                "type": "string",
                                "description": "Exact text to find in the draft.",
                            },
                            "replace": {
                                "type": "string",
                                "description": "Replacement text.",
                            },
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
EDITOR_PREAMBLE = (
    "You are the uncensored editor. The assistant message above is a draft "
    "that needs improvement based on the issues described below. Use the "
    "provided tools to apply the required changes."
)

# Sent when only audit issues are flagged (banned phrases, repetitive
# openers/templates) — no length guard.  Directs the model to patch only.
EDITOR_PATCH_INSTRUCTIONS = (
    "Use `editor_apply_patch` to apply a patch to fix flagged issues.\n\n"
    "PATCHING RULES:\n"
    "- The `search` field must be copied EXACTLY from the draft text above — including all punctuation and quotes.\n"
    "- Each patch must target a DIFFERENT, non-overlapping piece of text.\n"
    "- Do NOT send a patch where `search` and `replace` are identical.\n"
    "- For banned phrases: completely rewrite the sentence to eliminate the banned phrase. Make a creative and bold effort; do not just substitute with similar, related words. Note: The audit report may show the canonical phrase name, but you need to remove the actual variant that appears in the sentence.\n"
    "- For repetitive openers: rewrite and replace flagged sentences so they no longer begin with the same opening words. Vary the sentence structure.\n"
    "- For repetitive templates: restructure flagged sentences so they no longer follow the same POS pattern. Change clause order, combine sentences, or vary syntax.\n"
    "- For contrastive negation ('not X, but Y'): rewrite sentences that use this cliché construction. Consider alternative phrasing that avoids this rhetorical formula.\n\n"
    "Skip a fix only if the flagged text does not appear verbatim in the draft above."
)

# Sent when only the length guard is triggered — no audit issues.
# Directs the model to rewrite only.
EDITOR_REWRITE_INSTRUCTIONS = (
    "Use `editor_rewrite` to produce a rewrite within the specified limits.\n\n"
    "REWRITING RULES:\n"
    "- Preserve the author's vocabulary and creative word choices and all key story beats. Sentence starters should be varied - avoid 3+ consecutive sentences starting with the same word (e.g. 'she, she, she').\n"
    "- First priority is to get rid of repetitiveness and condense comma-separated adjectives into stronger, more precise words (e.g. old, ruined building -> decrepit building).\n"
    "- Be more concise but maintain coherence and narrative flow."
)

# Sent when both audit issues AND length guard are triggered.
# The model already receives the full audit report and length-guard
# instruction with concrete word/paragraph limits.
EDITOR_BOTH_INSTRUCTIONS = (
    "Call `editor_rewrite` to address both concerns in a single rewrite.\n"
    "- Address all audit issues while also respecting length constraints."
)

LENGTH_GUARD_INSTRUCTIONS = (
    "LENGTH GUARD: The draft is {word_count} words — too long. "
    "Call `editor_rewrite` with a rewrite: at most {max_paragraphs} paragraphs "
    "and {max_words} words. Preserve the author's voice and all key story beats."
)

# CoT prompt appended once at the end of the assembled editor prompt.
EDITOR_COT_PROMPT = (
    "Before calling the tool, state in one sentence which tool you'll call "
    "and the single most important change, then call the tool."
)

MAX_EDITOR_ITERATIONS = 3


# ── Tool registry & helpers

TOOLS: dict[str, dict] = {
    "direct_scene": {
        "choice": {"type": "function", "function": {"name": "direct_scene"}},
        "schema": AGENT_TOOLS[0],
    },
    "rewrite_user_prompt": {
        "choice": {"type": "function", "function": {"name": "rewrite_user_prompt"}},
        "schema": REWRITE_PROMPT_TOOL,
    },
    "editor_apply_patch": {
        "choice": {"type": "function", "function": {"name": "editor_apply_patch"}},
        "schema": EDITOR_APPLY_PATCH_TOOL,
    },
    "editor_rewrite": {
        "choice": {"type": "function", "function": {"name": "editor_rewrite"}},
        "schema": EDITOR_REWRITE_TOOL,
    },
}

PRE_WRITER_TOOLS = {"rewrite_user_prompt"}
POST_WRITER_TOOLS = {"editor_apply_patch", "editor_rewrite"}
ALL_SCHEMAS = [t["schema"] for t in TOOLS.values()]


def enabled_schemas(enabled_tools: dict | None) -> list[dict]:
    """Return tool schemas. None means 'all enabled', {} means 'all disabled'."""
    if enabled_tools is None:
        return ALL_SCHEMAS
    return [TOOLS[n]["schema"] for n in TOOLS if enabled_tools.get(n, False)]
