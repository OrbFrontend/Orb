"""Concrete built-in schemas, choices, and dynamic schema builders."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# Agent tool definitions.

# Fixed parameters always present in direct_scene.
_DIRECT_SCENE_FIXED_PROPERTIES = {
    "moods": {
        "type": "array",
        "items": {"type": "string"},
        "description": "List of moods to activate. Leave empty for a neutral tone.",
    },
}

_DIRECT_SCENE_FIXED_REQUIRED: list[str] = []

# The catalog is supplied in the selection step's trailing context.
_ACTIVE_LOREBOOK_PROPERTY = {
    "selected_lorebook_entries": {
        "type": "array",
        "items": {"type": "string"},
        "description": ("Names of lorebook entries relevant to this scene. Leave empty if none apply."),
    },
}

_DIRECT_SCENE_DESCRIPTION = (
    "Call this to direct the scene. "
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


_PROPOSE_WORLD_CHANGES_DESCRIPTION = (
    "Propose changes to a World's long-term memory based on the exchange. Nothing is written until review; "
    "leave operations empty when no durable change occurred."
)

# The Dynamic Worlds proposal tool. Like `select_lorebook`, a fixed schema
# registered statically and enabled per-turn by a feature gate, never by the
# user's tool toggles. It chooses only between `constant` and `keywords`
# activation -- every other lorebook field keeps a safe default the user can
# edit afterwards through the normal reviewed path, which keeps this schema (and
# therefore the shared per-turn tool blob) small and stable.
#
# Every field is one more thing a model can get wrong, so this asks only for
# what the model alone knows: `op` offers three verbs rather than the five the
# table stores (`validate_proposal` derives the stored one from the target row),
# and `rationale` comes first so a model emitting properties in schema order
# writes the justification before the change it justifies rather than after.
#
# Property order is load-bearing the other way round at the top level:
# `operations` precedes `summary`. The call is forced, so a model that writes a
# summary first has already declared a proposal exists, and an empty operations
# list then contradicts the sentence it just wrote -- it fills one in. Enumerate
# first, describe second, and proposing nothing stays available all the way
# through the call.
PROPOSE_WORLD_CHANGES_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_world_changes",
        "description": _PROPOSE_WORLD_CHANGES_DESCRIPTION,
        "parameters": {
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "description": "One entry per proposed change. Empty when nothing durable happened.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "rationale": {
                                "type": "string",
                                "description": "Why this belongs in long-term memory rather than only in the chat history.",
                            },
                            "op": {
                                "type": "string",
                                "enum": ["create", "revise", "retract"],
                                "description": (
                                    "create: something no entry covers yet. revise: the entry named below is "
                                    "now wrong, supply what it should say instead. retract: the entry named below "
                                    "no longer holds and nothing takes its place."
                                ),
                            },
                            "target_entry_id": {
                                "type": "integer",
                                "description": (
                                    "The id of the entry being revised or retracted, exactly as listed in the "
                                    "catalog. Omit for create."
                                ),
                            },
                            "target_world": {
                                "type": "string",
                                "description": (
                                    "For create only: the stable world_id shown in the destination World's "
                                    "catalog heading. Required when the catalog lists more than one World. "
                                    "Omit for every other op -- those go wherever the entry they name already is."
                                ),
                            },
                            "name": {
                                "type": "string",
                                "description": (
                                    "Short title for the entry, e.g. the person, place or fact it covers. Omit for retract."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": "The note itself, stated plainly in one or two sentences. Omit for retract.",
                            },
                            "activation": {
                                "type": "string",
                                "enum": ["constant", "keywords"],
                                "description": (
                                    "constant: something that must be known on every turn. "
                                    "keywords: about one person, place or thing, shown when it comes up."
                                ),
                            },
                            "keywords": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Words that should bring this entry back. Required for keywords activation.",
                            },
                        },
                        "required": ["rationale", "op"],
                    },
                },
                "summary": {
                    "type": "string",
                    "description": "One short sentence describing the operations listed above, for the review card.",
                },
            },
            "required": [],
        },
    },
}

PROPOSE_WORLD_CHANGES_CHOICE = {"type": "function", "function": {"name": "propose_world_changes"}}


_GIVE_FEEDBACK_DESCRIPTION = (
    "Step out of character and give the user an OOC note about the reply that was "
    "just written. This note will be shown to the user."
)


def _build_fragment_tool(name: str, description: str, fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build a tool schema whose parameters are exactly one string per fragment.

    Shared by the fragment-driven tools: each fragment contributes one string
    parameter keyed by its id, and there are no fixed parameters. Returns an
    OpenAI function-calling format dict.

    These schemas ride the shared per-turn tools blob (via ``schema_overrides``)
    so their step can force ``tool_choice`` on the tool without a cache miss.
    """
    properties: dict = {}
    required: list[str] = []

    for df in fragments:
        fid = df["id"]
        properties[fid] = {"type": "string", "description": df["description"]}
        if df.get("required"):
            required.append(fid)

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def build_feedback_tool(feedback_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the ``give_feedback`` tool schema from the enabled feedback fragments."""
    return _build_fragment_tool("give_feedback", _GIVE_FEEDBACK_DESCRIPTION, feedback_fragments)


GIVE_FEEDBACK_CHOICE = {"type": "function", "function": {"name": "give_feedback"}}


_RECORD_DIRECTION_NOTE_DESCRIPTION = (
    "Record lasting director notes that persist for the rest of the roleplay - once recorded, a "
    "note returns on every later reply and steers the story from here on. Each parameter is one "
    "category of note; fill only the categories that have something genuinely new and lasting to "
    "record this turn, and leave the rest empty."
)


def build_direction_note_tool(direction_note_fragments: Sequence[Mapping[str, Any]]) -> dict:
    """Build the ``record_direction_note`` tool schema from the enabled direction-note fragments."""
    return _build_fragment_tool("record_direction_note", _RECORD_DIRECTION_NOTE_DESCRIPTION, direction_note_fragments)


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
        "description": (
            "Apply one or more replacements to the draft. Each patch identifies a numbered finding from the "
            "Writing Audit Report by its id number and supplies the replacement text for that sentence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "integer",
                                "description": "The number of the sentence being fixed, as shown in [brackets] in the report.",
                            },
                            "replace": {
                                "type": "string",
                                "description": "Replacement text for that sentence.",
                            },
                        },
                        "required": ["id", "replace"],
                    },
                    "description": "One patch per numbered finding.",
                }
            },
            "required": ["patches"],
        },
    },
}
