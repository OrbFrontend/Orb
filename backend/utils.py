"""
utils.py — Shared helpers.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, TypedDict

from .llm_types import ContentPart


class LengthGuard(TypedDict):
    """Resolved length-guard limits threaded through the pipeline.

    Built by the orchestrator only when the length guard is enabled, so its mere
    presence *is* the on/off state — ``None`` means disabled, and any non-None
    value means enabled. Consumed by the writer (preventive nudge, only when
    ``enforce``) and the editor (corrective rewrite). ``enforce`` carries the
    enforce-mode flag so it travels with the limits instead of as a sidecar.
    """

    enforce: bool
    max_words: int
    max_paragraphs: int


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


def extract_hyperparams(settings: Mapping[str, Any], *, defaults: Mapping[str, Any] | None = None) -> dict:
    """Extract LLM hyperparameters from a settings dict.

    Optionally fills in *defaults* for any keys not present in settings.
    """
    keys = [
        "temperature",
        "max_tokens",
        "top_p",
        "min_p",
        "top_k",
        "repetition_penalty",
    ]
    params = {k: v for k in keys if (v := settings.get(k)) is not None}
    if defaults:
        for k, v in defaults.items():
            if k not in params:
                params[k] = v
    return params


def build_multimodal_content(text: str, attachments: Optional[Sequence[Mapping[str, Any]]] = None) -> str | list[ContentPart]:
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
