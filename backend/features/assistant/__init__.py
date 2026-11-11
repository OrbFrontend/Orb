"""The right-panel assistant: a bounded tool loop over callables its caller supplies."""

from __future__ import annotations

from .contracts import (
    MAX_ITEM_CHARS,
    MAX_REPLAY_ITEMS,
    MAX_RESULT_CHARS,
    MAX_TOOL_EXECUTIONS,
    MAX_VIEW_LABEL_CHARS,
    AssistantEvent,
    AssistantTool,
    AssistantUnavailable,
    HistoryItem,
    NoArguments,
    ToolArguments,
    ToolFailure,
    ToolOutput,
    ToolSet,
    ViewContext,
)
from .loop import run_assistant, settled
