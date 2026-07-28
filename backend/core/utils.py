"""
utils.py — Shared helpers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

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


def build_multimodal_content(
    text: str,
    attachments: Sequence[Mapping[str, Any]] | None = None,
    trailing_text: str = "",
) -> str | list[ContentPart]:
    """Wrap *text* (and optional image attachments) into a multimodal content list.

    Returns a plain string when there are no attachments, or a list of content
    parts suitable for vision-capable LLM endpoints.

    *trailing_text* is appended as a final text part **after** the images. It
    exists for content that has to be the semantic tail of the message: a
    policy block placed before the images would be separated from the model's
    turn boundary by every attachment, and "the last thing you read" is the
    property such a block is relying on. With no attachments it simply
    concatenates, which is the same tail in the degenerate case.
    """
    if not attachments:
        return text + trailing_text
    parts: list[ContentPart] = [{"type": "text", "text": text}]
    for att in attachments:
        mime = att.get("mime_type", att.get("mime", "image/jpeg"))
        b64 = att.get("data_b64", att.get("b64", ""))
        if not b64:
            continue
        url = f"data:{mime};base64,{b64}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    if trailing_text:
        parts.append({"type": "text", "text": trailing_text})
    return parts
