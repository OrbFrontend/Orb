"""
tool_registry.py — Built-in tool schemas and the tool registry for the orchestrator pipeline.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

# ── Agent tool definitions (OpenAI function-calling format)

# Fixed parameters always present in direct_scene regardless of interactive fragments.
# Only moods is fixed; all other parameters come from interactive fragments.
_DIRECT_SCENE_FIXED_PROPERTIES = {
    "moods": {
        "type": "array",
        "items": {"type": "string"},
        "description": "List of moods to activate. Leave empty for a neutral tone.",
    },
}

_DIRECT_SCENE_FIXED_REQUIRED: list[str] = []

# Optional activation parameter added when the Agentic Lorebook feature is on.
# The schema declares only the *parameter*; the catalog of selectable values
# lives in the director OOC trailing (so the cached tools blob grows by a fixed
# ~1 property). Kept out of `required` so the Director may select none.
_ACTIVE_LOREBOOK_PROPERTY = {
    "selected_lorebook_entries": {
        "type": "array",
        "items": {"type": "string"},
        "description": ("Names of lorebook entries relevant to this scene. Leave empty if none apply."),
    },
}

_DIRECT_SCENE_DESCRIPTION = (
    "Call this to direct the scene. Deduce what the user wants to see and show them. "
    "Be very specific and intentional with the direction. Aim to keep things fresh, may churn if need to."
)


def build_direct_scene_tool(
    interactive_fragments: Sequence[Mapping[str, Any]],
    *,
    agentic_lorebook: bool = False,
) -> dict:
    """Build the direct_scene tool schema from enabled interactive fragments.

    Interactive fragments provide dynamic string/array parameters beyond the fixed
    moods and keywords fields. When *agentic_lorebook* is true, the fixed
    ``selected_lorebook_entries`` array parameter is appended so the Director can activate
    lorebook entries by name (the selectable catalog rides the director OOC, not
    this schema). The returned dict is in OpenAI function-calling format.
    """
    properties: dict = {}
    required: list[str] = []

    for df in interactive_fragments:
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
    if agentic_lorebook:
        properties.update(_ACTIVE_LOREBOOK_PROPERTY)
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


_GIVE_FEEDBACK_DESCRIPTION = (
    "Step out of character and give the user an out-of-character note about the reply that was "
    "just written. This note is shown to the user, not used to write the story."
)


def build_feedback_tool(feedback_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the give_feedback tool schema from enabled feedback fragments.

    Each ``field_type="feedback"`` interactive fragment contributes a single
    string parameter (keyed by fragment id); there are no fixed parameters. The
    returned dict is in OpenAI function-calling format.

    ``give_feedback`` is registered in ``TOOLS`` (internal, feedback-flag-gated)
    so its schema rides the shared per-turn tools blob exactly like
    ``direct_scene``: the orchestrator builds it once from the enabled feedback
    fragments and threads it via ``schema_overrides`` to every pass, keeping the
    blob byte-identical. The post-writer feedback step then just forces
    ``tool_choice=give_feedback`` on the unchanged shared base — no cache miss.
    """
    properties: dict = {}
    required: list[str] = []

    for df in feedback_fragments:
        fid = df["id"]
        properties[fid] = {"type": "string", "description": df["description"]}
        if df.get("required"):
            required.append(fid)

    return {
        "type": "function",
        "function": {
            "name": "give_feedback",
            "description": _GIVE_FEEDBACK_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


GIVE_FEEDBACK_CHOICE = {"type": "function", "function": {"name": "give_feedback"}}


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
    # Internal, feedback-flag-gated (never user-toggleable, like editor_rewrite).
    # The empty-properties placeholder schema is always overridden per-turn via
    # schema_overrides with build_feedback_tool(feedback_fragments) when feedback
    # is enabled; registering it here is what lets enabled_schemas() emit its
    # bytes into the shared blob so the feedback step reuses the cached base.
    "give_feedback": {
        "choice": GIVE_FEEDBACK_CHOICE,
        "schema": build_feedback_tool([]),
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
        "give_feedback",
    }
)
assert BUILTIN_TOOL_NAMES == frozenset(TOOLS.keys()), "BUILTIN_TOOL_NAMES drift vs TOOLS literal keys"

# Built-in tools partitioned by pipeline phase. PRE = director (pre-writer) tools;
# POST = editor + feedback (post-writer) tools. give_feedback is a post-writer
# feedback-step tool (passes/editor/feedback.py): it rides the shared per-turn tools
# blob (Invariant 3) but must NOT be offered to or triggered by the director.
PRE_WRITER_TOOLS = {"direct_scene", "rewrite_user_prompt"}
POST_WRITER_TOOLS = {"editor_apply_patch", "editor_rewrite", "give_feedback"}

assert PRE_WRITER_TOOLS.isdisjoint(POST_WRITER_TOOLS), "phase sets overlap"
assert PRE_WRITER_TOOLS | POST_WRITER_TOOLS == BUILTIN_TOOL_NAMES, "phase sets must partition built-ins"

# Tools registered with standalone=True are filtered out of the schemas array
# returned by enabled_schemas(). They remain reachable via direct tool_choice
# calls.
STANDALONE_TOOLS: set[str] = set()


def register_tool(name: str, schema: dict, choice: dict, *, standalone: bool = False) -> None:
    """Register or replace a tool. Symmetric on the standalone bit."""
    TOOLS[name] = {"schema": schema, "choice": choice}
    if standalone:
        STANDALONE_TOOLS.add(name)
    else:
        STANDALONE_TOOLS.discard(name)


def enabled_schemas(
    enabled_tools: Mapping[str, bool] | None,
    overrides: Mapping[str, dict] | None = None,
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
