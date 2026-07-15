"""
tool_registry.py — Built-in tool schemas and the tool registry.
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

# The Agentic Lorebook selection parameter. It is the sole parameter of the
# standalone `select_lorebook` tool (see below); the schema declares only the
# *parameter* — the catalog of selectable values rides the select step's OOC
# trailing. Kept out of `required` so the Director may select none.
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
) -> dict:
    """Build the ``direct_scene`` tool schema from the enabled interactive fragments.

    Fragments add dynamic string/array parameters beyond the fixed ``moods``
    field. Returns an OpenAI function-calling format dict. (Lorebook selection is
    a separate concern handled by the standalone ``select_lorebook`` tool.)
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


_SELECT_LOREBOOK_DESCRIPTION = (
    "Pick the lorebook entries relevant to the current scene from the catalog provided. "
    "Activate the ones that genuinely apply; leave the selection empty if none do."
)

# The agentic-lorebook selection tool: a fixed, fragment-independent schema, so it
# is registered statically (unlike direct_scene, which is rebuilt per turn from the
# enabled fragments). Its single parameter is the shared `_ACTIVE_LOREBOOK_PROPERTY`.
SELECT_LOREBOOK_TOOL = {
    "type": "function",
    "function": {
        "name": "select_lorebook",
        "description": _SELECT_LOREBOOK_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": dict(_ACTIVE_LOREBOOK_PROPERTY),
            "required": [],
        },
    },
}

SELECT_LOREBOOK_CHOICE = {"type": "function", "function": {"name": "select_lorebook"}}


_GIVE_FEEDBACK_DESCRIPTION = (
    "Step out of character and give the user an out-of-character note about the reply that was "
    "just written. This note is shown to the user, not used to write the story."
)


def build_feedback_tool(feedback_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the ``give_feedback`` tool schema from the enabled feedback fragments.

    Each ``field_type="feedback"`` fragment contributes one string parameter
    (keyed by fragment id); there are no fixed parameters. Returns an OpenAI
    function-calling format dict.

    The schema rides the shared per-turn tools blob (via ``schema_overrides``)
    so the post-writer feedback step can force ``tool_choice=give_feedback``
    without a cache miss.
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


_RECORD_DIRECTION_NOTE_DESCRIPTION = (
    "Record lasting director notes that persist for the rest of the roleplay -- once recorded, a "
    "note returns on every later reply and steers the story from here on. Each parameter is one "
    "category of note; fill only the categories that have something genuinely new and lasting to "
    "record this turn, and leave the rest empty."
)


def build_direction_note_tool(direction_note_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the ``record_direction_note`` tool schema from the enabled direction-note fragments.

    Each ``field_type="direction_note"`` fragment contributes one string parameter
    (keyed by fragment id); there are no fixed parameters. Returns an OpenAI
    function-calling format dict.

    The schema rides the shared per-turn tools blob (via ``schema_overrides``) so
    the direction-note step can force ``tool_choice=record_direction_note`` without
    a cache miss.
    """
    properties: dict = {}
    required: list[str] = []

    for df in direction_note_fragments:
        fid = df["id"]
        properties[fid] = {"type": "string", "description": df["description"]}
        if df.get("required"):
            required.append(fid)

    return {
        "type": "function",
        "function": {
            "name": "record_direction_note",
            "description": _RECORD_DIRECTION_NOTE_DESCRIPTION,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


RECORD_DIRECTION_NOTE_CHOICE = {"type": "function", "function": {"name": "record_direction_note"}}


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
    # Internal, mode-gated (never user-toggleable). The empty-properties placeholder
    # is overridden per-turn via schema_overrides with build_direction_note_tool(direction_note_
    # fragments); registering it here emits its bytes into the shared blob so the
    # direction-note step reuses the cached base.
    "record_direction_note": {
        "choice": RECORD_DIRECTION_NOTE_CHOICE,
        "schema": build_direction_note_tool([]),
    },
    # Internal, flag-gated (never user-toggleable). Enabled for the turn when the
    # Agentic Lorebook feature is active (see _build_writer_tools_blob); its fixed
    # schema rides the shared blob so the select step reuses the cached base. The
    # selectable catalog rides the select step's OOC trailing, not this schema.
    "select_lorebook": {
        "choice": SELECT_LOREBOOK_CHOICE,
        "schema": SELECT_LOREBOOK_TOOL,
    },
}

# Built-in tool names declared as a literal and asserted equal to TOOLS keys at
# module load so the two cannot drift silently if a contributor edits one
# without the other.
BUILTIN_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "direct_scene",
        "editor_apply_patch",
        "editor_rewrite",
        "give_feedback",
        "record_direction_note",
        "select_lorebook",
    }
)
assert BUILTIN_TOOL_NAMES == frozenset(TOOLS.keys()), "BUILTIN_TOOL_NAMES drift vs TOOLS literal keys"

# Built-in tools partitioned into two sets so the director's interactive loop knows
# which tools it may offer. PRE = the director loop's own tools (it iterates these
# and calls them itself). POST = everything else: the post-writer editor tools AND
# the internal forced-step tools that ride the shared per-turn blob (Invariant 3)
# but must NOT be offered to or triggered by the director loop — give_feedback
# (post-writer feedback step), record_direction_note (its own step, pre- or
# post-writer), and select_lorebook (the pre-writer agentic-lorebook select step).
# So "POST" here means "not a director-loop tool," not a literal pipeline phase.
PRE_WRITER_TOOLS = {"direct_scene"}
POST_WRITER_TOOLS = {"editor_apply_patch", "editor_rewrite", "give_feedback", "record_direction_note", "select_lorebook"}

assert PRE_WRITER_TOOLS.isdisjoint(POST_WRITER_TOOLS), "phase sets overlap"
assert PRE_WRITER_TOOLS | POST_WRITER_TOOLS == BUILTIN_TOOL_NAMES, "phase sets must partition built-ins"

# Tools registered with standalone=True are filtered out of the schemas array
# returned by enabled_schemas(). They remain reachable via direct tool_choice
# calls.
STANDALONE_TOOLS: set[str] = set()


def register_tool(name: str, schema: dict, choice: dict, *, standalone: bool = False) -> None:
    """Register or replace a tool in the registry."""
    TOOLS[name] = {"schema": schema, "choice": choice}
    if standalone:
        STANDALONE_TOOLS.add(name)
    else:
        STANDALONE_TOOLS.discard(name)


def enabled_schemas(
    enabled_tools: Mapping[str, bool] | None,
    overrides: Mapping[str, dict] | None = None,
) -> list[dict]:
    """Return schemas for enabled, non-standalone tools in registry order.

    ``enabled_tools=None`` returns every non-standalone schema. A dict
    filters to entries with a truthy value. ``overrides`` replaces named
    schemas with dynamic variants (e.g. the per-turn ``give_feedback``
    schema); an override value of ``None`` drops that tool from the result.
    """
    overrides = overrides or {}
    eligible = [n for n in TOOLS if n not in STANDALONE_TOOLS]
    if enabled_tools is not None:
        eligible = [n for n in eligible if enabled_tools.get(n, False)]
    return [s for n in eligible if (s := overrides.get(n, TOOLS[n]["schema"])) is not None]
