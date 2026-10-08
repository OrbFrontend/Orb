"""Instruction prompts owned by Editor sub-steps."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from typing import Any

from ....analysis.detectors.subject_fixation import PRESENT, SubjectStreak
from ....analysis.subjects import SUBJECT_DESCRIPTIONS
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

POST_PROCESSING_PREAMBLE = (
    "[OOC: You are the uncensored editor. Edit the assistant draft above for the task below. "
    "Call editor_find_replace exactly once."
)

POST_PROCESSING_RULES = (
    "FIND-AND-REPLACE RULES:\n"
    "- Copy each `find` value exactly from your current draft only, including case, whitespace, and punctuation.\n"
    "- Keep each find as short as practical while still identifying exactly one span.\n"
    "- Return all useful edits in one `patches` array; do not rewrite the entire draft.\n"
    "- Leave `replace` empty when the text in `find` should be deleted.\n"
)

SUBJECT_FIXATION_INSTRUCTION = (
    "Cut the harmful subjects below from your single most recent reply's narration with small edits; "
    "keep dialogue, key actions and new information. If nothing can go, return an empty `patches` array."
)

EDITOR_PATCH_INSTRUCTIONS = (
    "Use `editor_apply_patch` to apply a patch to fix ALL flagged issues.\n\n"
    "PATCHING RULES:\n"
    "- Each issue in the report below is numbered. The `id` field must be the number of the issue you are fixing.\n"
    "- Emit one patch per issue — do not skip any, and do not patch the same id twice.\n"
    "- `replace` is the new text for that span. Do not copy the old sentence into it.\n"
    "- The replacement text must be complete and make sense in the context."
)

# One rule per audit category (``Target.categories``), rendered only for the
# categories the report flags: each numbered issue already names its problem.
PATCH_CATEGORY_RULES: dict[str, str] = {
    "banned_phrases": "For banned phrases: completely rewrite the sentence to eliminate the banned phrase. Make a creative and bold effort; do not just substitute with similar words.",
    "repetitive_openers": "For repetitive openers: rewrite flagged sentences so they no longer begin with the same opening words. Vary the sentence structure.",
    "repetitive_templates": "For repetitive templates: restructure flagged sentences so they no longer follow the same POS pattern. Change clause order, combine sentences, vary syntax.",
    "phrase_repetition": "For repetitive phrases: rewrite flagged phrases, changing the subject.",
    "contrastive_negation": "For contrastive negation ('not X, but Y'): rewrite sentences that use this cliché construction. Consider alternative phrasing that avoids this rhetorical formula.",
    "anti_echo": "For interrogative dialogue: replace the dialogue with something entirely different.",
    "negated_narration": "For negated narration: remove descriptions of what does not happen. Only write actions that register and matter.",
}

# Categories whose rule also applies when a length or structural check chooses a full rewrite.
REWRITE_RULE_CATEGORIES = frozenset({"negated_narration"})

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
    feedback_fragments: Sequence[Mapping[str, Any]], reasoning_on: bool = False, tool_schema: dict | None = None
) -> str:
    """Build the post-Writer feedback request."""
    preamble = FEEDBACK_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    if tool_schema is not None:
        labels = {fragment["id"]: (fragment.get("injection_label") or "").strip() for fragment in feedback_fragments}
        # The shared schema carries feedback fields by name only; state each live one here.
        fragments = {fragment["id"]: fragment for fragment in feedback_fragments}
        parts.append(tool_call_instruction("give_feedback", tool_schema, labels=labels, fragments=fragments))
    return "\n\n".join(parts) + "]"


def build_post_processing_prompt(fragment: Mapping[str, Any], *, reasoning_on: bool = False) -> str:
    """Build one fragment-defined Editor request.

    The constant rules precede the per-fragment task so consecutive fragment
    calls share them as cached prefix whenever the draft came through unchanged.
    """
    preamble = POST_PROCESSING_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    heading = str(fragment.get("injection_label") or "").strip()
    instruction = str(fragment.get("description") or "").strip()
    return "\n\n".join([preamble, POST_PROCESSING_RULES, f"## {heading}", instruction]) + "]"


def build_subject_fixation_prompt(streaks: Sequence[SubjectStreak], *, reasoning_on: bool = False) -> str:
    """Build an exact-edit request listing each subject to cut."""
    targets = "\n".join(
        f"- {'Any mention' if streak.level == PRESENT else 'Descriptions'} of {SUBJECT_DESCRIPTIONS.get(streak.category, streak.category)}"
        for streak in streaks
    )
    task = {"injection_label": "Subject fixation", "description": f"{SUBJECT_FIXATION_INSTRUCTION}\n\n{targets}"}
    return build_post_processing_prompt(task, reasoning_on=reasoning_on)


def _category_rules(categories: Collection[str]) -> str:
    """The rule lines for *categories*, in ``PATCH_CATEGORY_RULES`` order."""
    return "\n".join(f"- {rule}" for category, rule in PATCH_CATEGORY_RULES.items() if category in categories)


def patch_instructions(categories: Collection[str], *, shown: Collection[str] | None = None) -> str:
    """The patching instructions for a report flagging *categories*.

    *shown* is what the conversation already carries: ``None`` when no patch instructions have been sent yet, else the
    categories whose rules have been. Only what is missing is returned, so a replayed tool result can add the rules for kinds
    that surface after the first request.
    """
    rules = _category_rules(set(categories).difference(shown or ()))
    if shown is not None:
        return rules
    return EDITOR_PATCH_INSTRUCTIONS + ("\n" + rules if rules else "")


def editor_patches(has_audit_issues: bool, length_guard_triggered: bool, structural_rewrite: bool, patchable: bool) -> bool:
    """Whether the Editor request asks for patches rather than a rewrite."""
    return has_audit_issues and patchable and not (length_guard_triggered or structural_rewrite)


def build_editor_prompt(
    has_audit_issues: bool,
    report_text: str,
    length_guard_triggered: bool,
    length_guard_instruction: str,
    structural_rewrite: bool = False,
    reasoning_on: bool = False,
    patchable: bool = True,
    patch_categories: Collection[str] = (),
) -> str:
    """Assemble the Editor's request message.

    *patch_categories* are the audit categories the report flags; a patch request
    carries only their rules, and a rewrite request only their content rules.
    """
    preamble = EDITOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]

    if editor_patches(has_audit_issues, length_guard_triggered, structural_rewrite, patchable):
        parts.append(patch_instructions(patch_categories))
        parts.append(report_text)
    elif length_guard_triggered or structural_rewrite:
        parts.append(EDITOR_REWRITE_INSTRUCTIONS)
        if has_audit_issues:
            parts.append(report_text)
            if rules := _category_rules(REWRITE_RULE_CATEGORIES.intersection(patch_categories)):
                parts.append(rules)
        if structural_rewrite:
            parts.append(STRUCTURAL_REWRITE_INSTRUCTIONS)
        if length_guard_triggered:
            parts.append(length_guard_instruction)
        if has_audit_issues and length_guard_triggered:
            parts.append(EDITOR_BOTH_INSTRUCTIONS)
    return "\n\n".join(parts) + "]"
