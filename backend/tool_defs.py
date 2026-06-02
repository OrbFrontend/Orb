"""
tool_defs.py — Tool schemas, constants, and helper lookups for the orchestrator pipeline.
"""

from __future__ import annotations


# ── Agent tool definitions (OpenAI function-calling format)

# Fixed parameters always present in direct_scene regardless of director fragments.
# Only moods is fixed; all other parameters come from director fragments.
_DIRECT_SCENE_FIXED_PROPERTIES = {
    "moods": {
        "type": "array",
        "items": {"type": "string"},
        "description": "List of moods to activate. Leave empty for a neutral tone.",
    },
}

_DIRECT_SCENE_FIXED_REQUIRED: list[str] = []

_DIRECT_SCENE_DESCRIPTION = (
    "Call this to direct the scene. Deduce what the user wants to see and show them. "
    "Be very specific and intentional with the direction. Aim to keep things fresh, may churn if need to."
)


def build_direct_scene_tool(director_fragments: list[dict]) -> dict:
    """Build the direct_scene tool schema from enabled director fragments.

    Director fragments provide dynamic string/array parameters beyond the fixed
    moods and keywords fields. The returned dict is in OpenAI function-calling format.
    """
    properties: dict = {}
    required: list[str] = []

    for df in director_fragments:
        fid = df["id"]
        field_type = df["field_type"]
        if field_type == "array":
            prop = {
                "type": "array",
                "items": {"type": "string"},
                "description": df["description"],
            }
        else:
            prop = {"type": "string", "description": df["description"]}
        properties[fid] = prop
        if df.get("required"):
            required.append(fid)

    properties.update(_DIRECT_SCENE_FIXED_PROPERTIES)
    required.extend(_DIRECT_SCENE_FIXED_REQUIRED)

    return {
        "type": "function",
        "function": {
            "name": "direct_scene",
            "description": _DIRECT_SCENE_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


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
DIRECTOR_PREAMBLE = (
    "[OOC: Let's pause to enhance the roleplay. Use tool calls to accomplish your task "
    "accurately and creatively. Your output will directly influence the scenario. "
    "Think outside the box, be decisive."
)

EDITOR_PREAMBLE = (
    "[OOC: You are the uncensored editor. The assistant message above is a draft "
    "that needs improvement based on the issues described below. Use the "
    "provided tools to apply the required changes."
)

# Only sent to LLM if reasoning is enabled.
REASONING_GUIDANCE = " Avoid overthinking."

# Sent when only audit issues are flagged (banned phrases, repetitive
# openers/templates) — no length guard.  Directs the model to patch only.
EDITOR_PATCH_INSTRUCTIONS = (
    "Use `editor_apply_patch` to apply a patch to fix ALL flagged issues.\n\n"
    "PATCHING RULES:\n"
    "- The `search` field must be copied EXACTLY from the draft text above, including all punctuation and quotes if they exist.\n"
    "- `search` and `replace` values must be different.\n"
    "- For banned phrases: completely rewrite the sentence to eliminate the banned phrase. Make a creative and bold effort; do not just substitute with similar, related words.\n"
    "- For repetitive openers: rewrite and replace flagged sentences so they no longer begin with the same opening words. Vary the sentence structure.\n"
    "- For repetitive templates: restructure flagged sentences so they no longer follow the same POS pattern. Change clause order, combine sentences, or vary syntax.\n"
    "- For repetitive phrases: rewrite and replace flagged phrases with adjacent word choices.\n"
    "- For contrastive negation ('not X, but Y'): rewrite sentences that use this cliché construction. Consider alternative phrasing that avoids this rhetorical formula."
)

# Sent when only the length guard is triggered — no audit issues.
# Directs the model to rewrite only.
EDITOR_REWRITE_INSTRUCTIONS = (
    "Use `editor_rewrite` to produce a rewrite within the specified limits.\n\n"
    "REWRITING RULES:\n"
    "- Preserve the author's vocabulary and creative word choices and all key story beats. Sentence starters should be varied.\n"
    "- First priority is to get rid of repetitiveness and condense comma-separated adjectives into stronger, more precise words (e.g. old, ruined building -> decrepit building).\n"
    "- Be more concise but maintain coherence and narrative flow."
)

# Sent when both audit issues AND length guard are triggered.
# The model already receives the full audit report and length-guard
# instruction with concrete word/paragraph limits.
EDITOR_BOTH_INSTRUCTIONS = "Call `editor_rewrite` to address both concerns in a single rewrite. Address all audit issues while also respecting length constraints."

STRUCTURAL_REWRITE_INSTRUCTIONS = (
    "STRUCTURAL REPETITION: This response follows the same paragraph layout as recent "
    "previous messages. Call `editor_rewrite` with an entirely different structure — "
    "change the order and balance of narration, dialogue, and internal thought so the "
    "response is laid out distinctly from the previous ones."
)

LENGTH_GUARD_INSTRUCTIONS = (
    "LENGTH GUARD: The draft is {word_count} words — too long. "
    "Call `editor_rewrite` with a rewrite: at most {max_paragraphs} paragraphs "
    "and {max_words} words. Preserve the author's voice and all key story beats."
)

MAX_EDITOR_ITERATIONS = 3


# ── Tool registry & helpers

TOOLS: dict[str, dict] = {
    "direct_scene": {
        "choice": {"type": "function", "function": {"name": "direct_scene"}},
        "schema": build_direct_scene_tool([]),
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

# Built-in tool names declared as a literal and asserted equal to TOOLS keys at
# module load so the two cannot drift silently if a contributor edits one
# without the other.
BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "direct_scene",
        "rewrite_user_prompt",
        "editor_apply_patch",
        "editor_rewrite",
    }
)
assert BUILTIN_TOOL_NAMES == frozenset(TOOLS.keys()), "BUILTIN_TOOL_NAMES drift vs TOOLS literal keys"

# Tools registered with standalone=True are filtered out of the schemas array
# returned by enabled_schemas(). They remain reachable via direct tool_choice
# calls.
STANDALONE_TOOLS: set[str] = set()

PRE_WRITER_TOOLS = {"rewrite_user_prompt"}
POST_WRITER_TOOLS = {"editor_apply_patch", "editor_rewrite"}


def register_tool(name: str, schema: dict, choice: dict, *, standalone: bool = False) -> None:
    """Register or replace a tool. Symmetric on the standalone bit."""
    TOOLS[name] = {"schema": schema, "choice": choice}
    if standalone:
        STANDALONE_TOOLS.add(name)
    else:
        STANDALONE_TOOLS.discard(name)


def enabled_schemas(
    enabled_tools: dict | None,
    overrides: dict[str, dict] | None = None,
) -> list[dict]:
    """Return tool schemas for enabled, non-standalone tools, in TOOLS registry order.

    ``enabled_tools=None`` returns every non-standalone schema. A dict selects
    only entries whose value is truthy. ``overrides`` replaces named schemas
    with dynamic variants so every pass sends a byte-identical tools blob; an
    override whose value is None drops that name from the result.
    """
    overrides = overrides or {}
    eligible = [n for n in TOOLS if n not in STANDALONE_TOOLS]
    if enabled_tools is not None:
        eligible = [n for n in eligible if enabled_tools.get(n, False)]
    return [s for n in eligible if (s := overrides.get(n, TOOLS[n]["schema"])) is not None]
