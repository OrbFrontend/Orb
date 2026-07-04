"""
prompt_builder.py — Assembles system prompts, lorebook blocks, style
injections, and tool-call request messages for the pipeline passes.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..core import ChatMessage, ContentPart, Macros
from .tool_registry import TOOLS


def format_message_with_attachments(message: Mapping[str, Any], macros: Macros | None) -> ChatMessage:
    """Convert a message dict to OpenAI chat format, embedding attachments.

    Two attachment lists on the message dict are handled differently:
      - ``user_attachments``: embedded as multimodal ``image_url`` parts in
        the message content.
      - ``workflow_attachments``: their raw bytes never enter the prefix; only
        the ``annotation`` of root rows (``parent_attachment_id IS NULL``) is
        appended as text. Sibling variants and blank annotations contribute nothing.

    Returns ``{"role": ..., "content": str | list}``.
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
    """Render the "call ONLY this tool, in schema order" instruction line.

    Echoes the tool description and parameter order from *schema*. When
    *labels* is given, each param id is annotated with its human-readable
    heading (used by the feedback step; the director passes ``None``).
    Single source for this wording so it can't drift between callers.
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
    "- For contrastive negation ('not X, but Y'): rewrite sentences that use this cliché construction. Consider alternative phrasing that avoids this rhetorical formula.\n"
    "- For interrogative dialogue: replace the dialogue AND its related narration with something entirely different."
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

    Used by :func:`build_director_tool_prompt` for the ``rewrite_user_prompt`` branch.
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
    # Close the [OOC: aside opened in DIRECTOR_PREAMBLE; the whole instruction is the aside.
    return "\n\n".join(parts) + "]"


def _render_decided(value: Any) -> str:
    return ", ".join(str(x) for x in value) if isinstance(value, list) else str(value)


def build_director_scene_step_prompt(
    user_message: str,
    active_moods: list[str],
    mood_fragments: Sequence[Mapping[str, Any]],
    *,
    tool_schema: dict | None = None,
    reasoning_on: bool = False,
    target_fragment: Mapping[str, Any] | None = None,
    decided_fields: Sequence[tuple[str, Any]] = (),
    progressive_prior: Any = None,
) -> str:
    """Build one ``direct_scene`` request that targets a single output.

    With ``target_fragment`` None the model is asked only for ``moods``; otherwise
    only for the named fragment, with the values already chosen this turn
    (``decided_fields``) shown so it can build on them.
    """
    schema = tool_schema if tool_schema is not None else TOOLS["direct_scene"]["schema"]
    desc = schema["function"]["description"]
    parts = [DIRECTOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")]

    if target_fragment is None:
        parts.append(f"Call ONLY direct_scene - {desc}\nFill ONLY: moods.")
        moods = ", ".join(active_moods) or "none"
        frags = "\n".join(f"* [{f['id']}] - use in case: {f['description']}" for f in mood_fragments)
        parts.append(f"Previously active moods: {moods}\n\nAvailable writing moods:\n{frags}")
    else:
        fid = target_fragment["id"]
        hint = {"array": "list of strings", "progressive": "single value, evolves across turns"}.get(
            target_fragment["field_type"], "single value"
        )
        parts.append(
            f"Call ONLY direct_scene - {desc}\nFill ONLY the '{fid}' parameter. Leave moods and all other fields empty."
        )
        parts.append(f"Field '{fid}' ({hint}): {target_fragment['description']}")
        prior = [f"- {label}: {_render_decided(value)}" for label, value in decided_fields if value]
        if prior:
            parts.append("Decided so far this turn (build on these, do not contradict):\n" + "\n".join(prior))
        if target_fragment["field_type"] == "progressive" and progressive_prior:
            parts.append(f"Previous value (update it): {progressive_prior}")

    parts.append(f'User\'s next message (context):\n"""{user_message}"""')
    # Close the [OOC: aside opened in DIRECTOR_PREAMBLE; the whole instruction is the aside.
    return "\n\n".join(parts) + "]"


def build_lorebook_select_prompt(catalog: str, user_message: str, *, reasoning_on: bool = False) -> str:
    """Build the request for the standalone agentic-lorebook ``select_lorebook`` step.

    The catalog of selectable entries rides this OOC trailing (not the system prompt
    or the tools blob), so the call reuses the shared history KV the other passes warm.
    The pending *user_message* trails the catalog: during the director pass it is not
    yet in the shared history, so without it the model can't judge relevance (it would
    only see the prior turn). Placed last so the stable preamble+catalog prefix caches.
    """
    parts = [
        DIRECTOR_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else ""),
        (
            "Call ONLY select_lorebook. From the catalog below, choose ONLY the entries relevant to the "
            "current scene and the user's next message (quoted after the catalog); leave the selection "
            "empty if none apply."
        ),
        catalog,
        f'User\'s next message:\n"""{user_message}"""',
    ]
    # Close the [OOC: aside opened in DIRECTOR_PREAMBLE; the whole instruction is the aside.
    return "\n\n".join(parts) + "]"


def build_feedback_prompt(
    feedback_fragments: Sequence[Mapping[str, Any]],
    reasoning_on: bool = False,
    tool_schema: dict | None = None,
) -> str:
    """Build the request message for the post-writer feedback step.

    The just-written reply is already in the message history as an assistant
    message, so it is not quoted here. *tool_schema* is the dynamic
    ``give_feedback`` schema; its parameter order is echoed via
    :func:`_tool_call_instruction` so the model fills fields in schema order.
    Each param id is paired with its ``injection_label`` (the heading the user
    sees) so the model understands what each opaque id means. Labels live in
    the per-turn request, not the tools blob, so the shared KV cache is untouched.
    """
    preamble = FEEDBACK_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    if tool_schema is not None:
        labels = {df["id"]: (df.get("injection_label") or "").strip() for df in feedback_fragments}
        parts.append(_tool_call_instruction("give_feedback", tool_schema, labels=labels))
    # Close the [OOC: aside opened in FEEDBACK_PREAMBLE; the whole instruction is the aside.
    return "\n\n".join(parts) + "]"


DIRECTION_NOTE_PREAMBLE = (
    "[OOC: Pause the roleplay and step out of character. The categories below are standing records "
    "of lasting direction for this roleplay, and your task now is to update them. Work through each "
    "one and record into it anything from what just happened that must hold for the rest of the "
    "roleplay. Whatever you record is permanent: it returns on every later reply and steers the "
    "rest of the story, so a category takes only what genuinely must constrain what follows -- if "
    "nothing this turn belongs in a category, leave it empty. Record only the bare fact in each "
    "category -- no leading label, category name, or turn number. Those are attached automatically; "
    "where earlier entries appear tagged that way, the tag is for your reference only."
)


def _direction_notes_lines(notes: Sequence[Mapping[str, Any]]) -> str:
    """One line per note in the given order (oldest-first, i.e. turn order), each tagged
    with its authoring fragment's label and the turn it was recorded on."""
    lines = []
    for n in notes:
        turn = n.get("turn_index")
        tag = f"{n['interactive_fragment_label']}, turn {turn}" if turn is not None else n["interactive_fragment_label"]
        lines.append(f"- ({tag}) {n['content']}")
    return "\n".join(lines)


def render_direction_notes_block(notes: Sequence[Mapping[str, Any]]) -> str:
    """Render the active direction notes as a Scene Direction sub-block, or '' when empty.

    Notes are listed in turn order, each prefixed with the label of the fragment that
    authored it so the writer can tell which directive a note belongs to.
    """
    if not notes:
        return ""
    return f"**Direction Notes**\n{_direction_notes_lines(notes)}"


def build_direction_note_prompt(
    active_notes: Sequence[Mapping[str, Any]],
    direction_note_fragments: Sequence[Mapping[str, Any]],
    *,
    inj_block: str | None = None,
    reasoning_on: bool = False,
    tool_schema: dict | None = None,
) -> str:
    """Build the request message for the direction-note step.

    *active_notes* are already in effect on this branch; they are listed in turn order
    (each labelled with its fragment) so the model evolves them rather than restating
    them. *inj_block* is this turn's scene direction, passed for the pre-writer placement;
    the post-turn placement omits it because the finished reply is already replayed in the
    message history. Each parameter id is paired with its fragment's label so the model
    knows what each opaque category id means.
    """
    preamble = DIRECTION_NOTE_PREAMBLE + (REASONING_GUIDANCE if reasoning_on else "")
    parts = [preamble]
    if active_notes:
        parts.append("Already recorded (do not repeat these):\n" + _direction_notes_lines(active_notes))
    if inj_block:
        parts.append(inj_block)
    if tool_schema is not None:
        labels = {df["id"]: (df.get("injection_label") or df.get("label") or "").strip() for df in direction_note_fragments}
        parts.append(_tool_call_instruction("record_direction_note", tool_schema, labels=labels))
    # Close the [OOC: aside opened in DIRECTION_NOTE_PREAMBLE; the whole request is the aside,
    # so the bracket closes at the very end, not inside the preamble.
    return "\n\n".join(parts) + "]"


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

    # Close the [OOC: aside opened in EDITOR_PREAMBLE; the whole instruction is the aside.
    return "\n\n".join(parts) + "]"


def build_patch_target_prompt(span: str, why: str) -> str:
    """Per-finding editor request for the text-mode prefill path.

    One flagged sentence per call; the ``search`` string is prefilled
    transport-side, so the model only writes the replacement. Self-contained
    (names the sentence and the issue) so the chat-transport fallback still
    yields a usable patch.
    """
    return (
        EDITOR_PREAMBLE
        + f"\n\nFlagged issue: {why}."
        + f"\n\nFlagged sentence:\n{span}"
        + "\n\nCall `editor_apply_patch` with exactly one patch whose `search` is that sentence, "
        "copied EXACTLY from the draft. Rewrite it boldly to fix the issue — do not just swap in "
        "similar words. Keep the surrounding narrative flow; an empty `replace` deletes the sentence.]"
    )


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
    """Compute the Scene Direction injection block from director pass outputs.

    When *direct_scene_enabled* is ``False``, mood signals and extra fields are
    cleared so the previous turn's director state cannot bleed into the writer.
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
    """Render the Scene Direction block for the writer pass.

    Interactive fragment values are rendered in ``sort_order`` using each
    fragment's ``injection_label``. Array fields become bullet lists.
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
