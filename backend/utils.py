"""
utils.py — Shared helpers.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional, Sequence, TypedDict


class LengthGuard(TypedDict):
    """Resolved length-guard limits threaded through the pipeline.

    Built by the orchestrator only when the length guard is enabled (``None``
    otherwise) and consumed by the writer and editor passes. ``enabled`` mirrors
    that on/off state so a hook receiving the dict need not re-derive it.
    """

    enabled: bool
    max_words: int
    max_paragraphs: int


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


def build_multimodal_content(text: str, attachments: Optional[Sequence[Mapping[str, Any]]] = None) -> str | list:
    """Wrap *text* (and optional image attachments) into a multimodal content list.

    Returns a plain string when there are no attachments, or a list of content
    parts suitable for vision-capable LLM endpoints.
    """
    if not attachments:
        return text
    parts: list = [{"type": "text", "text": text}]
    for att in attachments:
        mime = att.get("mime_type", att.get("mime", "image/jpeg"))
        b64 = att.get("data_b64", att.get("b64", ""))
        if not b64:
            continue
        url = f"data:{mime};base64,{b64}"
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts
