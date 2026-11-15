"""The groups the assistant's tools are offered in, which the panel's options switch on and off."""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from ..contracts import AssistantTool


@dataclass(frozen=True)
class ToolCategory:
    """One group of tools. *id* is the name a request switches it by; *label* is what the model and the panel call it."""

    id: str
    label: str
    tools: frozenset[str]


# Offered whatever is switched off: where the user is, and a read-only search of the library.
ALWAYS_OFFERED = frozenset({"read_context", "query_library"})

# In the order the panel lists them. Each tool is in exactly one, with the reads its group's writes depend on.
CATEGORIES: tuple[ToolCategory, ...] = (
    ToolCategory(
        "characters",
        "Characters and personas",
        frozenset(
            {
                "list_characters",
                "read_character",
                "list_personas",
                "create_character",
                "create_persona",
                "set_active_persona",
                "edit_character",
                "edit_persona",
                "delete_character",
            }
        ),
    ),
    ToolCategory(
        "chats",
        "Chats and messages",
        frozenset(
            {
                "read_messages",
                "read_chat_state",
                "start_chat",
                "set_chat",
                "checkpoint_chat",
                "switch_branch",
                "edit_message",
                "delete_chat",
                "delete_message",
                "regenerate_reply",
                "rewrite_reply",
                "continue_chat",
                "send_as_user",
                "open_chat",
            }
        ),
    ),
    ToolCategory(
        "lore",
        "Worlds and lore",
        frozenset(
            {
                "list_worlds",
                "list_lore_entries",
                "list_active_lore_entries",
                "create_world",
                "create_lore_entry",
                "set_world",
                "set_lore_entry",
                "set_chat_world",
                "edit_lore_entry",
                "delete_world",
                "delete_lore_entry",
            }
        ),
    ),
    ToolCategory(
        "fragments",
        "Fragments",
        frozenset(
            {
                "list_fragments",
                "read_fragment",
                "create_mood_fragment",
                "create_interactive_fragment",
                "create_decision_fragment",
                "set_mood_fragment",
                "set_interactive_fragment",
                "set_decision_fragment",
                "edit_mood_fragment",
                "edit_interactive_fragment",
            }
        ),
    ),
    ToolCategory(
        "settings",
        "Settings and phrase bank",
        frozenset(
            {
                "read_settings",
                "set_pipeline_toggle",
                "set_length_guard",
                "list_banned_phrases",
                "add_banned_phrase",
                "delete_banned_phrase",
            }
        ),
    ),
    ToolCategory("documents", "Documents", frozenset({"list_documents", "read_document"})),
    ToolCategory("media", "Images and speech", frozenset({"create_image", "illustrate_reply", "generate_speech"})),
)

_CATEGORY_OF = {name: category.id for category in CATEGORIES for name in category.tools}


def offered(tools: Sequence[AssistantTool], categories: Collection[str] | None) -> list[AssistantTool]:
    """The tools of *tools* in the groups *categories* names, in their order; ``None`` is every group.

    A tool in no group is a declaration error: it would stay offered whatever the user switched off.
    """
    for tool in tools:
        if tool.name not in ALWAYS_OFFERED and tool.name not in _CATEGORY_OF:
            raise LookupError(f"Assistant tool {tool.name} is in no category")
    if categories is None:
        return list(tools)
    return [tool for tool in tools if tool.name in ALWAYS_OFFERED or _CATEGORY_OF[tool.name] in categories]


def withheld_labels(categories: Collection[str] | None) -> list[str]:
    """The labels of the groups *categories* leaves out, for the prompt to name."""
    if categories is None:
        return []
    return [category.label for category in CATEGORIES if category.id not in categories]
