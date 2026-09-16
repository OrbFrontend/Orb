"""In-memory health verdicts for ComfyUI styles."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from ..config import style_source

# Bound memory use if callers supply unbounded style ids.
_MAX_ENTRIES = 64

# style id -> {"detail", "fingerprint"}
_verdicts: dict[str, dict[str, Any]] = {}


def fingerprint(config: Mapping[str, Any], style: Mapping[str, Any]) -> str:
    """Return the configuration fingerprint for a style verdict."""
    source, provider = style_source(config, style)
    parts: list[Any] = [source, provider, style.get("workflow"), style.get("checkpoint"), style.get("model")]
    if source == "external_comfy":
        external = config.get("external_comfy")
        parts.append((external if isinstance(external, Mapping) else {}).get("api_url"))
    else:
        cloud = config.get("cloud")
        providers = (cloud if isinstance(cloud, Mapping) else {}).get("providers")
        entry = (providers if isinstance(providers, Mapping) else {}).get(provider)
        entry = entry if isinstance(entry, Mapping) else {}
        key = str(entry.get("api_key") or "")
        # Stands in for the credential's identity, so rotating a key retires the
        # verdict recorded against the old one. Never stored, transmitted, or
        # checked against anything -- `usedforsecurity=False` says so, and keeps
        # this off the password-hashing scanners that cannot tell a cache key
        # from a credential at rest.
        digest = hashlib.sha256(key.encode(), usedforsecurity=False).hexdigest()[:16] if key else ""
        parts += [entry.get("base_url"), digest]
    return "\x1f".join(str(part or "") for part in parts)


def record_failure(config: Mapping[str, Any], style: Mapping[str, Any], detail: str) -> None:
    """Remember that this style's workflow cannot render here, and why."""
    style_id = str(style.get("id") or "")
    if not style_id or not detail:
        return
    if style_id not in _verdicts and len(_verdicts) >= _MAX_ENTRIES:
        _verdicts.clear()
    _verdicts[style_id] = {
        "detail": detail[:400],
        "fingerprint": fingerprint(config, style),
    }


def record_success(config: Mapping[str, Any], style: Mapping[str, Any]) -> None:
    """Clear a matching verdict for a style."""
    style_id = str(style.get("id") or "")
    record = _verdicts.get(style_id)
    if record is not None and record["fingerprint"] == fingerprint(config, style):
        del _verdicts[style_id]


def verdict(config: Mapping[str, Any], style: Mapping[str, Any]) -> dict[str, Any] | None:
    """The standing finding against this style, or None if there is none or it is stale."""
    style_id = str(style.get("id") or "")
    record = _verdicts.get(style_id)
    if record is None:
        return None
    if record["fingerprint"] != fingerprint(config, style):
        del _verdicts[style_id]
        return None
    return {
        "style_id": style_id,
        "style_label": str(style.get("label") or style_id),
        "detail": record["detail"],
    }


def forget(style_id: str | None = None) -> None:
    """Drop one verdict, or all of them. For tests and for a config reset."""
    if style_id is None:
        _verdicts.clear()
    else:
        _verdicts.pop(style_id, None)
