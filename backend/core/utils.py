"""Shared utility helpers."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .domain_types import AgentLane
from .llm_types import ContentPart

#: Heuristic characters-per-token ratio used for rough context-size estimates.
#: This is the one convention referenced throughout (see AGENTS.md → Context
#: Management); keep all chars→token estimation going through ``estimate_tokens``
#: rather than re-spelling the constant.
CHARS_PER_TOKEN = 4


def estimate_tokens(chars: int) -> int:
    """Rough token estimate from a character count (min 1 for any non-empty text)."""
    if chars <= 0:
        return 0
    return max(1, round(chars / CHARS_PER_TOKEN))


def scrub_log(value: object) -> str:
    """Sanitize a value for safe inclusion in a log message (CWE-117).

    User-controlled values can carry newlines or carriage returns that would
    otherwise let an attacker forge extra log lines. Coerce to text and strip
    the line breaks so each value stays confined to a single log record.
    """
    return str(value).replace("\r", "").replace("\n", "")


#: The sampler/budget fields a settings row carries for a lane, in the order the
#: endpoint editor shows them.
_HYPERPARAM_KEYS = (
    "temperature",
    "max_tokens",
    "top_p",
    "min_p",
    "top_k",
    "repetition_penalty",
)


def extract_hyperparams(
    settings: Mapping[str, Any],
    *,
    lane: AgentLane = "writer",
    defaults: Mapping[str, Any] | None = None,
) -> dict:
    """Extract LLM hyperparameters from a settings dict for the lane making the call.

    The agent lane reads each key's ``agent_`` twin. ``get_settings`` overlays those
    from the agent endpoint's own model config, and only when a separate lane
    resolves, so single-model mode falls through to the writer's values -- which is
    the same endpoint it is calling. Passing the writer's lane to an agent call is
    not a harmless default: it sends one endpoint's preset to another. The fallback
    is per key rather than whole-row only as a guard for partial mappings; the six
    columns behind these keys are all NOT NULL, so a resolved agent lane carries
    every twin and an unresolved one carries none.

    ``max_tokens`` goes out exactly as configured, on every call. A call whose
    whole answer must fit in one reply gets no hidden raise: the setting is the
    only budget, and a reply cut at it is reported against that setting.

    Optionally fills in *defaults* for any keys not present in settings. Note that
    both ``settings`` and ``model_configs`` declare all six columns NOT NULL, so
    *defaults* only ever fires for a partial mapping, never for a real row -- it is
    not a way to override a configured value.
    """
    prefix = "agent_" if lane == "agent" else ""
    params: dict[str, Any] = {}
    for key in _HYPERPARAM_KEYS:
        value = settings.get(f"{prefix}{key}") if prefix else None
        if value is None:
            value = settings.get(key)
        if value is not None:
            params[key] = value
    if defaults:
        for k, v in defaults.items():
            if k not in params:
                params[k] = v
    return params


#: The ``max_tokens`` column default, for a partial mapping that carries no budget.
_DEFAULT_MAX_TOKENS = 4096


def agent_lane_max_tokens(settings: Mapping[str, Any]) -> int:
    """The agent lane's configured reply budget.

    The lane cascade is ``extract_hyperparams``'; this is the spelling for a
    caller that sets its own samplers and wants only the budget.
    """
    return int(extract_hyperparams(settings, lane="agent").get("max_tokens") or _DEFAULT_MAX_TOKENS)


def agent_lane_cut_off(settings: Mapping[str, Any]) -> str:
    """The sentence for an agent-lane reply that stopped at its budget.

    It names the field the user edits: the ``agent_`` twin is present only when a
    separate Agent lane resolves, and otherwise agent calls spend the Writer
    model's own Max Tokens.
    """
    label = "Agent Max Tokens" if settings.get("agent_max_tokens") is not None else "Max Tokens"
    return f"The model's reply was cut off at the {label} limit of {agent_lane_max_tokens(settings)}."


def build_multimodal_content(text: str, attachments: Sequence[Mapping[str, Any]] | None = None) -> str | list[ContentPart]:
    """Wrap *text* (and optional image attachments) into a multimodal content list.

    Returns a plain string when there are no attachments, or a list of content
    parts suitable for vision-capable LLM endpoints.
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
