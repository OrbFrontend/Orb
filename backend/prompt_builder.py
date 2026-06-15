"""
prompt_builder.py — Functions that assemble system prompts, style injections,
and tool-call prompts for the orchestrator pipeline.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from .llm_types import ChatMessage, ContentPart
from .macros import Macros
from .tool_registry import TOOLS

LOREBOOK_SCAN_DEPTH = 6
# The agentic fallback scan only looks at the current turn (previous assistant
# message + current user message), since the Director already saw the history.
AGENTIC_LOREBOOK_SCAN_DEPTH = 2


def format_message_with_attachments(message: Mapping[str, Any], macros: Macros | None) -> ChatMessage:
    """Convert a message dict with optional attachments to OpenAI vision format.

    Two attachment lists travel on the message dict:
      - 'user_attachments': bytes embed as multimodal image_url parts on the
        message content.
      - 'workflow_attachments': bytes never enter the prefix. The 'annotation'
        column on a root row (parent_attachment_id IS NULL) is appended to the
        message text with a blank-line separator. Sibling variants and rows
        with empty or whitespace-only annotations contribute nothing.

    Returns a dict with 'role' and 'content' (string or list of parts).
    """
    role = message["role"]
    raw = message.get("content", "")
    text = macros.resolve_prompt(raw) if macros else raw

    user_atts: list[dict] = list(message.get("user_attachments") or [])
    workflow_annotations: list[str] = []
    for att in message.get("workflow_attachments") or []:
        if att.get("parent_attachment_id") is not None:
            continue
        annot = att.get("annotation")
        if isinstance(annot, str) and annot.strip():
            workflow_annotations.append(annot)

    text_parts = [text] if text else []
    text_parts.extend(workflow_annotations)
    combined_text = "\n\n".join(text_parts)

    if not user_atts:
        return {"role": role, "content": combined_text}

    parts: list[ContentPart] = []
    if combined_text:
        parts.append({"type": "text", "text": combined_text})
    for att in user_atts:
        mime = att["mime_type"]
        b64 = att["data_b64"]
        url = f"data:{mime};base64,{b64}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return {"role": role, "content": parts}


# ── System-prompt prefix


def build_prefix(
    system_prompt: str,
    char_persona: str,
    char_scenario: str,
    mes_example: str = "",
    post_history_instructions: str = "",
    messages: Sequence[Mapping[str, Any]] | None = None,
    macros: Macros | None = None,
    user_description: str = "",
    *,
    extra_system_blocks: list[str] | None = None,
) -> list[ChatMessage]:
    resolve = macros.resolve_message if macros else (lambda t: t)
    resolved = {
        key: resolve(val)
        for key, val in {
            "persona": char_persona,
            "scenario": char_scenario,
            "mes_example": mes_example,
            "post_history": post_history_instructions,
            "user_desc": user_description,
        }.items()
    }

    parts = [system_prompt]
    if macros and macros.char:
        parts.append(f"\n\n## Character: {macros.char}")
    if resolved["persona"]:
        parts.append(f"\n{resolved['persona']}")
    if resolved["scenario"]:
        parts.append(f"\n\n## Scenario\n{resolved['scenario']}")
    if resolved["mes_example"]:
        mes = resolved["mes_example"]
        if "<START>" in mes:
            processed_example = mes.replace("<START>", "## Example Dialogue")
            parts.append(f"\n\n{processed_example}")
        else:
            parts.append(f"\n\n## Example Dialogue\n{mes}")
    if resolved["post_history"]:
        parts.append(f"\n\n## Additional Instructions\n{resolved['post_history']}")
    if resolved["user_desc"].strip():
        user_label = macros.user if macros else "User"
        parts.append(f"\n\n## User: {user_label}\n{resolved['user_desc']}")

    if extra_system_blocks:
        for block in extra_system_blocks:
            parts.append(f"\n\n{block}")

    processed_messages = [format_message_with_attachments(m, macros) for m in (messages or [])]

    system_message: ChatMessage = {"role": "system", "content": "".join(parts)}
    return [system_message] + processed_messages


# ── Tool-call prompt


def _tool_call_instruction(
    tool_name: str,
    schema: dict,
    *,
    labels: Mapping[str, str] | None = None,
) -> str:
    """Render the shared "call ONLY this tool, in schema order" instruction line.

    Echoes the tool description and the parameter order from *schema* so the model
    fills fields in schema order. When *labels* is given, each param id is annotated
    with its human heading (e.g. a fragment's injection_label) — used by the
    feedback step so the model knows what each opaque id means; the director passes
    none and gets bare ids. Single source for the wording so it can't drift between
    :func:`build_director_tool_prompt` and :func:`build_feedback_prompt`.
    """
    desc = schema["function"]["description"]
    params = schema["function"]["parameters"].get("properties", {})
    if not params:
        param_order = "N/A"
    elif labels:
        param_order = ", ".join(f'{k} ("{labels[k]}")' if labels.get(k) else k for k in params)
    else:
        param_order = ", ".join(params.keys())
    return (
        f"Call ONLY this tool, ensuring parameters follow the schema order: "
        f"{tool_name} - {desc}\nParameter order: ({param_order})"
    )


def build_lorebook_catalog(entries: Sequence[Mapping[str, Any]]) -> str:
    """Build the Director's lorebook catalog for the agentic-activation path.

    Lists each non-``constant`` candidate entry — ``name`` plus its trigger
    keywords (at most the first 5) — grouped by world. Constant entries are excluded (they are always
    injected and the Director does not manage them). Deterministic order: worlds
    in first-appearance order of the already priority/sort-ordered *entries*, and
    entries within a world in that same order. Returns ``""`` when there are no
    non-constant candidates (no catalog → no ``selected_lorebook_entries`` arg is offered).
    """
    candidates = [e for e in entries if not e.get("constant")]
    if not candidates:
        return ""

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for e in candidates:
        groups.setdefault(e.get("world_name") or "", []).append(e)

    parts = [
        "**Available Lorebook Entries** — activate the ones relevant to the scene via `selected_lorebook_entries`. Possible values are wrapped in square brackets."
    ]
    for world, items in groups.items():
        if world:
            parts.append(f"### {world}")
        for e in items:
            name = e.get("name", "")
            kws = ", ".join((e.get("keywords", []) or [])[:5])
            parts.append(f"- [{name}] — {kws}" if kws else f"- [{name}]")
    return "\n".join(parts)


# ── Instruction templates

# Always included — tells the model who it is and what the assistant
# message above represents.  Without this, the model sees the roleplay
# system prompt plus a bare instruction and wastes tokens reasoning
# about context.
DIRECTOR_PREAMBLE = (
    "[OOC: Let's pause to enhance the roleplay. Use tool calls to accomplish your task "
    "accurately and creatively. Your output will directly influence the scenario. "
    "Think outside the box, be decisive."
)

EDITOR_PREAMBLE = (
    "[OOC: You are the uncensored editor. The assistant message above is a draft "
    "that needs improvement based on the issues described below. Use the "
    "provided tools to apply the required changes."
)

FEEDBACK_PREAMBLE = (
    "[OOC: Let's pause the roleplay. Step out of character and act as a helpful "
    "game master speaking directly to the user. Based on the reply that was just written, "
    "give the user a short, concrete out-of-character note. Use the give_feedback tool. "
    "This note is for the user only — it will NOT be shown to the writer or affect the story."
)

# Only sent to LLM if reasoning is enabled.
REASONING_GUIDANCE = " Avoid overthinking."

# Sent when only audit issues are flagged (banned phrases, repetitive
# openers/templates) — no length guard.  Directs the model to patch only.
EDITOR_PATCH_INSTRUCTIONS = (
    "Use `editor_apply_patch` to apply a patch to fix ALL flagged issues.\n\n"
    "PATCHING RULES:\n"
    "- The `search` field must be copied EXACTLY from the draft text above, including all punctuation and quotes if they exist.\n"
    "- `search` and `replace` values must be different.\n"
    "- For banned phrases: completely rewrite the sentence to eliminate the banned phrase. Make a creative and bold effort; do not just substitute with similar, related words.\n"
    "- For repetitive openers: rewrite and replace flagged sentences so they no longer begin with the same opening words. Vary the sentence structure.\n"
    "- For repetitive templates: restructure flagged sentences so they no longer follow the same POS pattern. Change clause order, combine sentences, or vary syntax.\n"
    "- For repetitive phrases: rewrite and replace flagged phrases.\n"
    "- For contrastive negation ('not X, but Y'): rewrite sentences that use this cliché construction. Consider alternative phrasing that avoids this rhetorical formula."
)

# Sent when only the length guard is triggered — no audit issues.
# Directs the model to rewrite only.
EDITOR_REWRITE_INSTRUCTIONS = (
    "Use `editor_rewrite` to produce a rewrite within the specified limits.\n\n"
    "REWRITING RULES:\n"
    "- Preserve the author's vocabulary and creative word choices and all key story beats. Sentence starters should be varied.\n"
    "- First priority is to get rid of repetitiveness and condense comma-separated adjectives into stronger, more precise words (e.g. old, ruined building -> decrepit building).\n"
    "- Be more concise but maintain coherence and narrative flow."
)

# Sent when both audit issues AND length guard are triggered.
# The model already receives the full audit report and length-guard
# instruction with concrete word/paragraph limits.
EDITOR_BOTH_INSTRUCTIONS = "Call `editor_rewrite` to address both concerns in a single rewrite. Address all audit issues while also respecting length constraints."

STRUCTURAL_REWRITE_INSTRUCTIONS = (
    "STRUCTURAL REPETITION: This response follows the same paragraph layout as recent "
    "previous messages. Call `editor_rewrite` with an entirely different structure — "
    "change the order and balance of narration, dialogue, and internal thought so the "
    "response is laid out distinctly from the previous ones."
)

#: Per-turn request tail handed to the director when running the rewrite tool: the
#: user's raw message, quoted for the model to refine. Lives here (not in the tools
#: blob) so it can vary per turn without busting the KV cache — mirroring
#: ``LENGTH_GUARD_INSTRUCTIONS`` leaving ``tool_registry.py``.
REWRITE_PROMPT_PROMPT = 'User\'s message:\n"""[{user_message}]"""'


def build_rewrite_prompt(user_message: str) -> str:
    """Format :data:`REWRITE_PROMPT_PROMPT` with the raw *user_message*.

    Appended by :func:`build_director_tool_prompt` as the request tail for the
    ``rewrite_user_prompt`` branch.
    """
    return REWRITE_PROMPT_PROMPT.format(user_message=user_message)


def build_director_tool_prompt(
    tool_name: str,
    user_message: str,
    active_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    reasoning_on: bool = False,
    interactive_fragments: Sequence[Mapping[str, Any]] | None = None,
    progressive_state: dict | None = None,
    tool_schema: dict | None = None,
    lorebook_catalog: str = "",
) -> str:
    tool = TOOLS.get(tool_name)
    if not tool:
        return ""
    schema = tool_schema if tool_schema is not None else tool["schema"]
    preamble = DIRECTOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [
        preamble,
        _tool_call_instruction(tool_name, schema),
    ]
    if tool_name == "direct_scene":
        moods = ", ".join(active_moods) or "none"
        frags = "\n".join(f"* [{f['id']}] - use in case: {f['description']}" for f in mood_fragments)
        parts.append(f"Previously active moods: {moods}\n\nAvailable writing moods:\n{frags}")
        # Agentic lorebook catalog rides the OOC trailing (not the system prompt /
        # tools blob) so the Writer reuses the shared history KV the Director warms.
        if lorebook_catalog:
            parts.append(lorebook_catalog)
        progressive_lines = [
            f"* [{df['id']}] ({df['description']}): {(progressive_state or {}).get(df['id'])}"
            for df in (interactive_fragments or [])
            if df.get("field_type") == "progressive" and (progressive_state or {}).get(df["id"])
        ]
        if progressive_lines:
            parts.append("Previous progressive fields - dynamically update these:\n" + "\n".join(progressive_lines))
        parts.append(f'User\'s next message (for context, take this into account when directing):\n"""{user_message}"""')
    elif tool_name == "rewrite_user_prompt":
        parts.append(build_rewrite_prompt(user_message))
    return "\n\n".join(parts)


def build_feedback_prompt(
    feedback_fragments: Sequence[Mapping[str, Any]],
    reasoning_on: bool = False,
    tool_schema: dict | None = None,
) -> str:
    """Build the trailing *request* message for the post-writer feedback step.

    The just-written reply is supplied to the model as its own ``assistant``
    message (so the feedback step extends the writer/editor KV-cached stack
    instead of forking off the bare prefix — see :func:`feedback_step`), so it is
    deliberately NOT quoted here. *tool_schema* is the dynamic ``give_feedback``
    schema (from :func:`build_feedback_tool`); its parameter order is echoed via
    the shared :func:`_tool_call_instruction` (as in
    :func:`build_director_tool_prompt`) so the model fills fields in schema order.
    Each param id is additionally paired with its injection_label — the same
    human heading the user sees on the rendered note
    (chat_inspector.feedbackRows) — so the model knows what each field is for
    beyond the opaque id. The labels live in the per-turn request only, not the
    give_feedback schema, so the shared tools-blob KV cache is untouched.
    """
    preamble = FEEDBACK_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    if tool_schema is not None:
        labels = {df["id"]: (df.get("injection_label") or "").strip() for df in feedback_fragments}
        parts.append(_tool_call_instruction("give_feedback", tool_schema, labels=labels))
    return "\n\n".join(parts)


def build_editor_prompt(
    has_audit_issues: bool,
    report_text: str,
    length_guard_triggered: bool,
    length_guard_instruction: str,
    structural_rewrite: bool = False,
    reasoning_on: bool = False,
) -> str:
    preamble = EDITOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    rewrite_triggered = length_guard_triggered or structural_rewrite

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

    return "\n\n".join(parts)


# ── Style injection block


def compute_style_injection_block(
    active_moods: list[str],
    prior_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    interactive_fragments: Sequence[Mapping[str, Any]],
    direct_scene_enabled: bool,
    extra_fields: dict | None = None,
    prior_progressive_state: dict | None = None,
) -> str:
    """Compute the style injection block from director-pass outputs.

    When *direct_scene_enabled* is False, mood signals are suppressed so the
    previous turn's director state cannot bleed into the writer. extra_fields
    are also cleared since all fields (including keywords) now come fresh from
    the current director pass and are empty when it did not run.
    """
    if extra_fields is None:
        extra_fields = {}

    if direct_scene_enabled:
        inj_active_moods = active_moods
        inj_extra = extra_fields
    else:
        inj_active_moods = []
        inj_extra = {}

    deactivated = (
        [f for f in mood_fragments if f["id"] in (set(prior_moods) - set(inj_active_moods))]
        if direct_scene_enabled and inj_active_moods
        else []
    )
    active = [f for f in mood_fragments if f["id"] in inj_active_moods]

    if not (active or deactivated or inj_extra):
        return ""

    return build_style_injection(active, deactivated, interactive_fragments, inj_extra, prior_progressive_state)


def build_style_injection(
    active: Sequence[Mapping[str, Any]],
    deactivated: Sequence[Mapping[str, Any]] | None = None,
    interactive_fragments: Sequence[Mapping[str, Any]] | None = None,
    extra_fields: dict | None = None,
    prior_progressive_state: dict | None = None,
) -> str:
    """Render the Scene Direction injection block for the writer pass.

    Interactive fragment values are rendered in sort_order, each using the
    fragment's injection_label.  Arrays are rendered as bullet lists.
    """
    parts = ["**Scene Direction**"]

    for df in sorted(interactive_fragments or [], key=lambda x: x.get("sort_order", 0)):
        val = (extra_fields or {}).get(df["id"])
        if not val:
            continue
        label = df["injection_label"]
        if df["field_type"] == "array" and isinstance(val, list):
            parts.append(label + ":\n" + "\n".join(f"- {item}" for item in val))
        elif df["field_type"] == "progressive":
            old_val = (prior_progressive_state or {}).get(df["id"])
            transition = f"{old_val} -> {val}" if old_val and old_val != val else str(val)
            parts.append(f"{label} ({df['description']}): {transition}")
        else:
            parts.append(f"{label}: {val}")

    for f in active:
        parts.append(f["prompt_text"])
    for f in deactivated or []:
        if neg := f.get("negative_prompt", "").strip():
            parts.append(neg)

    return "\n\n".join(parts)


# ── Lorebook injection block


def compute_lorebook_injection_block(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Compute the lorebook injection block from active entries.

    Constant entries are always included. Other entries are included when
    one of their keywords appears in the 6 most recent messages (any role).

    Entries are sorted by priority DESC. Returns empty string if no matches.
    """
    if not entries:
        return ""

    return render_lorebook_block(select_keyword_entries(messages, entries), macros)


def select_keyword_entries(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    scan_depth: int = LOREBOOK_SCAN_DEPTH,
) -> list[Mapping[str, Any]]:
    """Select entries activated by the keyword/substring scan.

    Constant entries are always selected. Other entries are selected when one of
    their keywords appears (substring match) in the ``scan_depth`` most recent
    messages. Returns the matched entries in input order.
    """
    scan_parts = [m.get("content") or "" for m in messages[-scan_depth:] if m.get("content")]
    scan_text = " ".join(scan_parts)
    matched: list[Mapping[str, Any]] = []

    for entry in entries:
        if entry.get("constant"):
            matched.append(entry)
            continue

        keywords = entry.get("keywords", [])
        if not keywords or not scan_text:
            continue

        case_insensitive = entry.get("case_insensitive", True)
        text = scan_text.lower() if case_insensitive else scan_text

        found = False
        for kw in keywords:
            kw_text = kw.lower() if case_insensitive else kw
            if kw_text in text:
                found = True
                break

        if found:
            matched.append(entry)

    return matched


def render_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Render the ``**Lorebook**`` block from already-*selected* entries.

    Shared by both activation paths — the keyword scan
    (:func:`compute_lorebook_injection_block`) and the agentic Director
    (:func:`compute_agentic_lorebook_block`). Entries are sorted by priority
    DESC; names/content are macro-resolved. Returns ``""`` when *entries* is
    empty.
    """
    if not entries:
        return ""

    matched = sorted(entries, key=lambda e: e.get("priority", 100), reverse=True)

    resolve = macros.resolve_message if macros else (lambda t: t)
    parts = ["**Lorebook**"]
    for entry in matched:
        name = resolve(entry.get("name", ""))
        content = resolve(entry.get("content", ""))
        if name and content:
            parts.append(f"{name}: {content}")
        elif content:
            parts.append(content)

    return "\n\n".join(parts)


def compute_agentic_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    selected_names: Sequence[str],
    macros: Macros | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Render the ``**Lorebook**`` block from the Director's agentic selection.

    Selects ``constant`` entries (always injected) ∪ entries whose ``name``
    matches one of *selected_names* (compared case-insensitively, trimmed) ∪
    entries activated by the keyword/substring scan over the current turn of
    *messages* (``AGENTIC_LOREBOOK_SCAN_DEPTH``), run in parallel so a keyword
    the Director overlooks still activates its entry.
    Duplicate names activate every matching entry; names with no match are
    ignored. Renders via the shared :func:`render_lorebook_block`. Returns ``""``
    when nothing is selected.
    """
    if not entries:
        return ""

    director_named = {(n or "").strip().casefold() for n in (selected_names or [])}
    keyword_hit = {id(e) for e in select_keyword_entries(messages or [], entries, AGENTIC_LOREBOOK_SCAN_DEPTH)}

    def is_active(entry: Mapping[str, Any]) -> bool:
        name = (entry.get("name", "") or "").strip().casefold()
        return bool(entry.get("constant")) or name in director_named or id(entry) in keyword_hit

    selected = [e for e in entries if is_active(e)]
    return render_lorebook_block(selected, macros)
