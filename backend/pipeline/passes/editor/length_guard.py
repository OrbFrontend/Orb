"""
passes/editor/length_guard.py — The length-guard feature.

Caps response length in two arms:

* *Preventive* (writer): :func:`writer_nudge` appends a "keep it under N words"
  instruction to the writer's user message, only in enforce mode.
* *Corrective* (editor): :func:`evaluate_length_guard` checks the finished draft
  and, when it overshoots, gives the editor a directive to call ``editor_rewrite``.

:func:`resolve_length_guard` converts raw settings into the :class:`LengthGuard`
config; a non-None result means the guard is enabled. :func:`apply_length_guard_tools`
ensures ``editor_rewrite`` is present in every pass's tool blob.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict


class LengthGuard(TypedDict):
    """Resolved length-guard config threaded through the pipeline.

    Built only when the guard is enabled, so a non-None value means enabled and
    ``None`` means disabled. The writer uses it for the preventive nudge (only
    when ``enforce`` is True); the editor uses it for the corrective rewrite.
    """

    enforce: bool
    max_words: int
    max_paragraphs: int


#: Corrective directive handed to the editor when a draft overshoots its word
#: budget. ``editor_rewrite`` is the only tool that can satisfy it (a full-draft
#: replacement); the editor forces that tool via tool_choice when triggered.
LENGTH_GUARD_INSTRUCTIONS = (
    "LENGTH GUARD: The draft is {word_count} words — too long. "
    "Call `editor_rewrite` with a rewrite: at most {max_paragraphs} paragraphs "
    "and {max_words} words. Preserve the author's voice and all key story beats."
)


def resolve_length_guard(settings: Mapping[str, Any], agent_on: bool) -> LengthGuard | None:
    """Resolve the length-guard config from *settings*, or ``None`` when disabled.

    Agent-gated: returns ``None`` when the agent is off. The returned dict is the
    on/off state downstream — ``cfg.length_guard is not None`` means enabled.
    """
    if not agent_on or not bool(settings.get("length_guard_enabled", 0)):
        return None
    return {
        "enforce": bool(settings.get("length_guard_enforce", 0)),
        "max_words": int(settings.get("length_guard_max_words", 240)),
        "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
    }


def apply_length_guard_tools(enabled_tools: Mapping[str, bool], length_guard: LengthGuard | None) -> Mapping[str, bool]:
    """Add ``editor_rewrite`` to *enabled_tools* when the length guard is on.

    This is the only path that enables ``editor_rewrite`` (it is internal, not
    user-toggleable). Returns *enabled_tools* unchanged when the guard is off.
    """
    if length_guard is None:
        return enabled_tools
    return {**enabled_tools, "editor_rewrite": True}


def writer_nudge(length_guard: LengthGuard | None) -> str:
    """Return the writer's self-limiting instruction, or ``""`` when not in enforce mode.

    A non-None *length_guard* already means the guard is enabled; this fires only
    when ``enforce`` is also True.
    """
    if not length_guard or not length_guard["enforce"]:
        return ""
    return (
        f"**Keep your response under {length_guard['max_words']} words and {length_guard['max_paragraphs']} paragraphs.**\n\n"
    )


def evaluate_length_guard(draft: str, length_guard: LengthGuard | None) -> tuple[bool, str, int]:
    """Return whether *draft* overshoots its word budget.

    Returns ``(triggered, instruction, word_count)``. When triggered,
    *instruction* is the formatted directive the editor passes to the model
    (``editor_rewrite`` is forced via ``tool_choice``). A ``None`` guard or an
    in-budget draft yields ``(False, "", word_count)``.
    """
    if length_guard is None:
        return False, "", 0
    word_count = len(draft.split())
    if word_count <= length_guard["max_words"]:
        return False, "", word_count
    instruction = LENGTH_GUARD_INSTRUCTIONS.format(
        word_count=word_count,
        max_paragraphs=length_guard["max_paragraphs"],
        max_words=length_guard["max_words"],
    )
    return True, instruction, word_count
