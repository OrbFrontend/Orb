"""
passes/editor/length_guard.py — The length-guard feature, in one place.

Length guard caps response length in two arms:

* *Preventive* (writer): :func:`writer_nudge` appends a one-line "keep it under
  N words" instruction to the writer's user message, only in enforce mode.
* *Corrective* (editor): :func:`evaluate_length_guard` checks the finished draft
  and, when it overshoots, hands the editor a directive to call ``editor_rewrite``.

:func:`resolve_length_guard` turns raw settings into the :class:`LengthGuard`
config the orchestrator threads to both passes; a non-None result *is* the on/off
state. :func:`apply_length_guard_tools` carries the one cross-cutting requirement:
the feature needs the ``editor_rewrite`` tool present in every pass's schema blob.
"""

from __future__ import annotations

from typing import Any, Mapping, TypedDict


class LengthGuard(TypedDict):
    """Resolved length-guard limits threaded through the pipeline.

    Built by :func:`resolve_length_guard` only when the guard is enabled, so its
    mere presence *is* the on/off state — ``None`` means disabled, and any
    non-None value means enabled. Consumed by the writer (preventive nudge, only
    when ``enforce``) and the editor (corrective rewrite). ``enforce`` carries the
    enforce-mode flag so it travels with the limits instead of as a sidecar.
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

    Agent-gated: the guard is one of the agent-dependent features, so it is off
    whenever the agent is off (see ``agent_enabled``). The dict is built *only*
    when enabled, so its presence is the on/off state downstream —
    ``cfg.length_guard is not None`` means enabled.
    """
    if not agent_on or not bool(settings.get("length_guard_enabled", 0)):
        return None
    return {
        "enforce": bool(settings.get("length_guard_enforce", 0)),
        "max_words": int(settings.get("length_guard_max_words", 240)),
        "max_paragraphs": int(settings.get("length_guard_max_paragraphs", 4)),
    }


def apply_length_guard_tools(enabled_tools: Mapping[str, bool], length_guard: LengthGuard | None) -> Mapping[str, bool]:
    """Mirror the ``editor_rewrite`` tool into *enabled_tools* when the guard is on.

    The length-guard *feature* requires the ``editor_rewrite`` *tool*: enabling it
    here means ``enabled_schemas()`` includes its schema in all three passes — the
    same KV-cache approach as ``editor_apply_patch``. ``editor_rewrite`` is
    internal (not user-toggleable); this is its only enable path. Returns
    *enabled_tools* unchanged when the guard is off.
    """
    if length_guard is None:
        return enabled_tools
    return {**enabled_tools, "editor_rewrite": True}


def writer_nudge(length_guard: LengthGuard | None) -> str:
    """Preventive arm: the writer instruction to self-limit, or ``""``.

    Fires only in enforce mode (``length_guard["enforce"]``); a non-None
    *length_guard* already means the feature is enabled. The returned text is
    appended to the writer's user-message tail.
    """
    if not length_guard or not length_guard["enforce"]:
        return ""
    return (
        f"**Keep your response under {length_guard['max_words']} words and {length_guard['max_paragraphs']} paragraphs.**\n\n"
    )


def evaluate_length_guard(draft: str, length_guard: LengthGuard | None) -> tuple[bool, str, int]:
    """Corrective arm: decide whether *draft* overshoots its word budget.

    Returns ``(triggered, instruction, word_count)``. When triggered,
    *instruction* is the formatted :data:`LENGTH_GUARD_INSTRUCTIONS` directive the
    editor feeds the model (alongside forcing ``editor_rewrite`` via tool_choice).
    A ``None`` guard or an in-budget draft yields ``(False, "", word_count)``.
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
