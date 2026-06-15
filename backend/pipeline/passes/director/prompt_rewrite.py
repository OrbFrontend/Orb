"""
passes/director/prompt_rewrite.py — The user-prompt-rewrite feature, in one place.

The feature rewrites a vague user message into a fuller prompt before the writer
runs, via the ``rewrite_user_prompt`` tool. The director executes it *first* (see
:func:`order_director_tools`) so a user who dislikes the rewrite can abort before
the full director runs; its reasoning is suppressed (:func:`suppresses_reasoning`)
because the rephrase is mechanical and latency-sensitive. The orchestrator then
swaps in the rewritten text as the writer's effective message
(:func:`apply_rewrite`), emits the ``prompt_rewritten`` SSE event, and persists the
overwrite — those I/O steps stay in the orchestrator; this module is pure.

The tool *schema* deliberately stays in ``tool_registry.py`` (``REWRITE_PROMPT_TOOL``
in the ``TOOLS`` registry): it is part of the cached tools blob sent to the LLM, so
moving it would bust the KV cache. The instruction template and its formatter live
in ``prompt_builder.py`` (``build_rewrite_prompt``) next to the rest of prompt
assembly; only the Python glue around the tool relocates here — mirroring how
``length_guard.py`` leaves the ``editor_rewrite`` schema in ``tool_registry.py``.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

#: The tool name, as a single source of truth for the literal that the schema in
#: ``tool_registry.py`` registers and that the director/orchestrator key off.
REWRITE_TOOL_NAME = "rewrite_user_prompt"


def extract_rewritten_message(args: Mapping[str, Any]) -> str | None:
    """Pull the rewritten message out of a ``rewrite_user_prompt`` tool call.

    Returns the ``refined_message`` argument, or ``None`` when it is absent or
    empty (so a blank rewrite is treated as "no rewrite" downstream).
    """
    return args.get("refined_message") or None


def order_director_tools(tool_names: Iterable[str]) -> list[str]:
    """Order the director's tools so ``rewrite_user_prompt`` runs first.

    Rewrite-first lets a user who dislikes the rewrite abort early, before the
    full director (mood/scene direction) runs. ``direct_scene`` follows; any other
    tool sorts after both. The order is stable for unlisted tools.
    """
    priority = [REWRITE_TOOL_NAME, "direct_scene"]
    return sorted(tool_names, key=lambda x: priority.index(x) if x in priority else len(priority))


def suppresses_reasoning(name: str) -> bool:
    """Whether tool *name* should run with reasoning disabled.

    ``rewrite_user_prompt`` does: it is a mechanical rephrase, and suppressing its
    reasoning keeps the pre-writer latency the user waits through down.
    """
    return name == REWRITE_TOOL_NAME


def apply_rewrite(user_message: str, rewritten_msg: str | None) -> tuple[str, bool]:
    """Resolve the writer's effective message from the (optional) rewrite.

    Returns ``(effective_msg, did_rewrite)``: the rewritten text when present, else
    the original *user_message*, plus a flag for whether a rewrite actually
    happened. The orchestrator uses the flag to gate the ``prompt_rewritten`` SSE
    emit and the DB overwrite; this only computes the swap (no I/O here).
    """
    return (rewritten_msg or user_message, bool(rewritten_msg))


def disable_rewrite(enabled_tools: Mapping[str, bool]) -> dict[str, bool]:
    """Return *enabled_tools* with ``rewrite_user_prompt`` forced off.

    Used by super-regenerate, where the writer's input is an OOC steering message
    that must not be rewritten. Mirrors :func:`apply_length_guard_tools` in
    returning a fresh dict rather than mutating the input.
    """
    return {**enabled_tools, REWRITE_TOOL_NAME: False}
