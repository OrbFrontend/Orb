"""Instruction prompts owned by the Editor and feedback passes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .._prompting import REASONING_GUIDANCE, tool_call_instruction

EDITOR_PREAMBLE = (
    "[OOC: You are the uncensored editor. The assistant message above is a draft "
    "that needs improvement based on the issues described below. Use the "
    "provided tools to apply the required changes."
)

FEEDBACK_PREAMBLE = (
    "[OOC: Pause the scene. Step out of character and act as a "
    "game master speaking directly to the user. Based on the reply that was just written, "
    "give the user a short, concrete out-of-character note. Use the give_feedback tool."
)

EDITOR_PATCH_INSTRUCTIONS = (
    "Use `editor_apply_patch` to apply a patch to fix ALL flagged issues.\n\n"
    "PATCHING RULES:\n"
    "- Each issue in the report below is numbered. The `id` field must be the number of the issue you are fixing.\n"
    "- Emit one patch per issue — do not skip any, and do not patch the same id twice.\n"
    "- `replace` is the new text for that sentence. Do not copy the old sentence into it.\n"
    "- For banned phrases: completely rewrite the sentence to eliminate the banned phrase. Make a creative and bold effort; do not just substitute with similar, related words.\n"
    "- For repetitive openers: rewrite and replace flagged sentences so they no longer begin with the same opening words. Vary the sentence structure.\n"
    "- For repetitive templates: restructure flagged sentences so they no longer follow the same POS pattern. Change clause order, combine sentences, or vary syntax.\n"
    "- For repetitive phrases: rewrite and replace flagged phrases.\n"
    "- For contrastive negation ('not X, but Y'): rewrite sentences that use this cliché construction. Consider alternative phrasing that avoids this rhetorical formula.\n"
    "- For interrogative dialogue: replace the dialogue AND its related narration with something entirely different."
)

EDITOR_REWRITE_INSTRUCTIONS = (
    "Use `editor_rewrite` to produce a rewrite within the specified limits.\n\n"
    "REWRITING RULES:\n"
    "- Preserve the author's vocabulary and creative word choices and all key story beats. Sentence starters should be varied.\n"
    "- First priority is to get rid of repetitiveness and condense comma-separated adjectives into stronger, more precise words (e.g. old, ruined building -> decrepit building).\n"
    "- Be more concise but maintain coherence and narrative flow."
)

EDITOR_BOTH_INSTRUCTIONS = "Call `editor_rewrite` to address both concerns in a single rewrite. Address all audit issues while also respecting length constraints."

EDITOR_RENUMBER_NOTICE = (
    "The draft has changed and the issues below have been renumbered. Ignore the ids from your previous "
    "call and patch only the ids listed in this report."
)

STRUCTURAL_REWRITE_INSTRUCTIONS = (
    "STRUCTURAL REPETITION: This response follows the same paragraph layout as recent "
    "previous messages. Call `editor_rewrite` with an entirely different structure — "
    "change the order and balance of narration, dialogue, and internal thought so the "
    "response is laid out distinctly from the previous ones."
)


def build_feedback_prompt(
    feedback_fragments: Sequence[Mapping[str, Any]],
    reasoning_on: bool = False,
    tool_schema: dict | None = None,
) -> str:
    """Build the post-Writer feedback request."""
    preamble = FEEDBACK_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    if tool_schema is not None:
        labels = {fragment["id"]: (fragment.get("injection_label") or "").strip() for fragment in feedback_fragments}
        parts.append(tool_call_instruction("give_feedback", tool_schema, labels=labels))
    return "\n\n".join(parts) + "]"


def build_editor_prompt(
    has_audit_issues: bool,
    report_text: str,
    length_guard_triggered: bool,
    length_guard_instruction: str,
    structural_rewrite: bool = False,
    reasoning_on: bool = False,
    patchable: bool = True,
) -> str:
    """Assemble the Editor's request message."""
    preamble = EDITOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    rewrite_triggered = length_guard_triggered or structural_rewrite or (has_audit_issues and not patchable)

    if rewrite_triggered:
        parts.append(EDITOR_REWRITE_INSTRUCTIONS)
        if has_audit_issues:
            parts.append(report_text)
        if structural_rewrite:
            parts.append(STRUCTURAL_REWRITE_INSTRUCTIONS)
        if length_guard_triggered:
            parts.append(length_guard_instruction)
        if has_audit_issues and length_guard_triggered:
            parts.append(EDITOR_BOTH_INSTRUCTIONS)
    elif has_audit_issues:
        parts.append(EDITOR_PATCH_INSTRUCTIONS)
        parts.append(report_text)
    return "\n\n".join(parts) + "]"
