"""Shared utility helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .domain_types import AgentLane
from .llm_types import ContentPart
from .settings import Settings

#: Heuristic characters-per-token ratio used for rough context-size estimates.
#: This is the one convention referenced throughout (see AGENTS.md -> Context
#: Management); keep all chars->token estimation going through ``estimate_tokens``
#: rather than re-spelling the constant.
CHARS_PER_TOKEN = 4


def estimate_tokens(chars: int) -> int:
    """Rough token estimate from a character count (min 1 for any non-empty text)."""
    if chars <= 0:
        return 0
    return max(1, round(chars / CHARS_PER_TOKEN))


def scrub_log(value: object) -> str:
    """Sanitize a value for safe inclusion in a log message (CWE-117).

    User-controlled values can carry newlines or carriage returns that would otherwise let an attacker forge extra log lines.
    Coerce to text and strip the line breaks so each value stays confined to a single log record.
    """
    return str(value).replace("\r", "").replace("\n", "")


#: The sampler/budget fields a settings row carries for a lane, in the order the
#: endpoint editor shows them.
_HYPERPARAM_KEYS = ("temperature", "max_tokens", "top_p", "min_p", "top_k", "repetition_penalty")


def extract_hyperparams(settings: Settings, *, lane: AgentLane = "writer") -> dict:
    """Extract hyperparameters for the calling lane.

    Agent keys fall back per key to Writer values when absent. Explicit None omits a parameter. Every value goes out exactly as
    configured: no call substitutes its own sampler or raises the budget.
    """
    prefix = "agent_" if lane == "agent" else ""
    params: dict[str, Any] = {}
    for key in _HYPERPARAM_KEYS:
        lane_key = f"{prefix}{key}"
        value = settings.get(lane_key) if prefix and lane_key in settings else settings.get(key)
        if value is not None:
            params[key] = value
    return params


#: The ``max_tokens`` column default, for a partial mapping that carries no budget.
_DEFAULT_MAX_TOKENS = 4096


def agent_lane_max_tokens(settings: Settings) -> int:
    """The agent lane's configured reply budget.

    The lane cascade is ``extract_hyperparams``'; this is the spelling for a
    caller that sets its own samplers and wants only the budget.
    """
    return int(extract_hyperparams(settings, lane="agent").get("max_tokens") or _DEFAULT_MAX_TOKENS)


def agent_lane_cut_off(settings: Settings) -> str:
    """The sentence for an agent-lane reply that stopped at its budget.

    It names the field the user edits: the ``agent_`` twin is present only when a separate Agent lane resolves, and otherwise
    agent calls spend the Writer model's own Max Tokens.
    """
    label = "Agent Max Tokens" if settings.get("agent_max_tokens") is not None else "Max Tokens"
    return f"The model's reply was cut off at the {label} limit of {agent_lane_max_tokens(settings)}."


def build_multimodal_content(text: str, attachments: Sequence[Mapping[str, Any]] | None = None) -> str | list[ContentPart]:
    """Wrap *text* (and optional image attachments) into a multimodal content list.

    Returns a plain string when there are no attachments, or a list of content parts suitable for vision-capable LLM endpoints.
    """
    if not attachments:
        return text
    parts: list[ContentPart] = [{"type": "text", "text": text}]
    for att in attachments:
        mime = att.get("mime_type", att.get("mime", "image/jpeg"))
        b64 = att.get("data_b64", att.get("b64", ""))
        if not b64:
            continue
        url = f"data:{mime};base64,{b64}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts
