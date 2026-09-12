"""Reasoning protocols learned from rendered assistant messages, never raw Jinja.

The renderer supplies the bytes; this module recognizes supported stream shapes
and prepares their continuation state without changing the conversation prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

ThinkTags = tuple[str, str]
SplitterStart = Literal["auto", "reasoning", "content"]
GEMMA_TAGS: ThinkTags = ("<|channel>thought\n", "<channel|>")
THINK_TAGS: ThinkTags = ("<think>", "</think>")
MINIMAX_TAGS: ThinkTags = ("<mm:think>", "</mm:think>")
NO_TAGS: ThinkTags = ("", "")
SELF_HEADER = " to=self<|message|>"
REPLY_HEADER = " to=user<|message|>"

PROBE_REASONING = "ORB_REASONING_7f32"
PROBE_CONTENT = "ORB_CONTENT_7f32"


def probe_messages(*, history: bool = False, empty: bool = False) -> list[dict]:
    """Fixed synthetic inputs; no conversation data and no generation needed."""
    messages = [{"role": "user", "content": "Reply with one color."}]
    if history:
        messages.append(
            {
                "role": "assistant",
                "content": PROBE_CONTENT,
                "reasoning_content": "" if empty else PROBE_REASONING,
            }
        )
    return messages


@dataclass(frozen=True)
class ReasoningControls:
    """Rendered transitions, kept separate from the stream delimiters."""

    open: str
    reply: str
    close: str


@dataclass(frozen=True)
class ReasoningFormat:
    status: Literal["known", "none", "unknown"] = "none"
    tags: ThinkTags = NO_TAGS
    channel: bool = False
    controls: ReasoningControls | None = None
    detail: str = ""

    def require_supported(self) -> None:
        if self.status == "unknown":
            raise ReasoningFormatError(
                f"Cannot determine the text-completion reasoning format: {self.detail}. "
                "Retry after checking the server, or use Chat Completions mode."
            )


class ReasoningFormatError(RuntimeError):
    """Stop before generation when raw text cannot be classified reliably."""


_TAG_BEFORE_REASONING = re.compile(
    r"(<((?:[A-Za-z0-9_-]+:)?(?:think(?:ing)?|thought|reason(?:ing)?)(?::[A-Za-z0-9_-]+)?)>)(\s*)$"
)


def format_from_probes(*, on: str, off: str, history: str, empty_history: str) -> ReasoningFormat:
    """Recognize markers immediately surrounding the synthetic reasoning field.

    Requiring the empty render to agree prevents an incidental tag elsewhere in
    the prompt from authorizing prompt edits. Unsupported/ambiguous reasoning is
    distinct from a template that demonstrably ignores the reasoning field and
    toggle. No arbitrary template expressions are interpreted here.
    """
    unknown = ReasoningFormat(
        status="unknown",
        detail="the rendered reasoning boundaries are unsupported or ambiguous",
    )
    if history.count(PROBE_CONTENT) != 1 or empty_history.count(PROBE_CONTENT) != 1:
        return unknown
    if PROBE_REASONING not in history:
        return ReasoningFormat() if history == empty_history and on == off else unknown
    if history.count(PROBE_REASONING) != 1 or history.replace(PROBE_REASONING, "") != empty_history:
        return unknown
    before, after = history.split(PROBE_REASONING)
    if PROBE_CONTENT in before:
        return unknown
    between, _ = after.split(PROBE_CONTENT)
    if before.endswith(SELF_HEADER) and between.endswith(REPLY_HEADER):
        # Both headers and their transition were observed in the rendered
        # assistant message. Only this routed protocol uses ChannelSplitter.
        if between not in (
            "<|eom|><|start|>assistant" + REPLY_HEADER,
            "<|start|>assistant" + REPLY_HEADER,
        ):
            return unknown
        return ReasoningFormat(
            status="known",
            channel=True,
            controls=ReasoningControls(SELF_HEADER, REPLY_HEADER, between),
        )
    if before.endswith(GEMMA_TAGS[0]) and between.strip() == GEMMA_TAGS[1]:
        opening, closing = GEMMA_TAGS
        open_suffix = opening
    else:
        match = _TAG_BEFORE_REASONING.search(before)
        if not match:
            return unknown
        opening, name, padding = match.groups()
        closing = f"</{name}>"
        if between.strip() != closing:
            return unknown
        open_suffix = opening + padding
    return ReasoningFormat(
        status="known",
        tags=(opening, closing),
        controls=ReasoningControls(open_suffix, open_suffix + between, between),
    )


@dataclass(frozen=True)
class PreparedPrompt:
    text: str
    start: SplitterStart
    reasoning_prefill: str = ""


def prompt_state(prompt: str, fmt: ReasoningFormat, *, prefill: bool = False) -> SplitterStart:
    """Observed prompt state wins over the requested reasoning toggle."""
    if prefill or fmt.status == "none":
        return "content"
    controls = fmt.controls
    if controls:
        tail = prompt.rstrip()
        if tail.endswith(controls.reply.rstrip()):
            return "content"
        if not fmt.channel and fmt.tags[1] and tail.endswith(fmt.tags[1]):
            return "content"
        if tail.endswith(controls.open.rstrip()):
            return "reasoning"
    return "auto"


def select_reply(prompt: str, fmt: ReasoningFormat) -> str:
    """Select the reply at the tail; preserve all preceding prompt bytes."""
    controls = fmt.controls
    if not controls or prompt_state(prompt, fmt) == "content":
        return prompt
    if prompt_state(prompt, fmt) == "reasoning":
        # Replace only the observed open suffix with the rendered empty span.
        # Routed messages close through a message transition instead.
        if fmt.channel:
            return prompt + controls.close
        tail = prompt.rstrip()
        return tail[: -len(controls.open.rstrip())] + controls.reply
    return prompt + controls.reply


def prepare_prompt(
    prompt: str,
    fmt: ReasoningFormat,
    *,
    reasoning: bool,
    prefill: bool = False,
    reasoning_prefill: str = "",
    constrained: bool = False,
) -> PreparedPrompt:
    """Return the final bytes and the one state every stream parser must honor."""
    fmt.require_supported()
    start = prompt_state(prompt, fmt, prefill=prefill)
    controls = fmt.controls
    if reasoning_prefill and reasoning and not prefill and controls:
        if start == "content":
            raise ReasoningFormatError("The rendered prompt already selects a reply; cannot insert a reasoning prefill.")
        if start != "reasoning":
            prompt += controls.open
        return PreparedPrompt(prompt + reasoning_prefill, "reasoning", reasoning_prefill)
    if constrained and not prefill:
        # A grammar cannot emit routing/delimiter tokens. Close an observed
        # thought even when the template ignored a reasoning-off request.
        prompt = select_reply(prompt, fmt)
        start = "content"
    return PreparedPrompt(prompt, start)
