"""Deterministic cleaning of the judge's and analyzer's raw tool output.

Both functions take whatever the model emitted (already coerced to a Python
value by the tool-call parser, but otherwise untrusted) and return a clean,
typed result. They are the graceful-degradation boundary for these two tools: a
malformed reply yields fewer/empty results, never an exception.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def validate_violations(raw: Any, draft: str, filled_keys: Iterable[str]) -> list[dict]:
    """Keep only violations the enforcer can actually act on.

    A violation survives iff its ``excerpt`` is a non-empty string occurring
    verbatim in *draft* (so a patch can locate it) and its ``category`` is a
    filled element name (so it maps to a recorded rule). Duplicate
    ``(excerpt, category)`` pairs collapse to one. The verbatim check doubles as
    a hallucination filter, which is what makes the surviving count trustworthy
    enough to drive the loop's no-progress guard.
    """
    if not isinstance(raw, list):
        return []
    keys = set(filled_keys)
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        excerpt = v.get("excerpt")
        category = v.get("category")
        if not isinstance(excerpt, str) or not excerpt:
            continue
        if not isinstance(category, str) or category not in keys:
            continue
        if excerpt not in draft:
            continue
        key = (excerpt, category)
        if key in seen:
            continue
        seen.add(key)
        out.append({"excerpt": excerpt, "category": category})
    return out


def clean_analyzer_records(raw: Any, schema_keys: Iterable[str]) -> dict[str, str]:
    """Map the analyzer's records to ``{element: denotation}``, keeping only
    schema elements with a non-empty string denotation.

    The string guard is load-bearing: an un-guarded non-string denotation in
    ``values`` would later crash ``filled_elements``/``is_armed`` (and thus 500
    the menu's state read) the moment it is consulted.
    """
    if not isinstance(raw, list):
        return {}
    keys = set(schema_keys)
    out: dict[str, str] = {}
    for r in raw:
        if not isinstance(r, dict):
            continue
        category = r.get("category")
        denotation = r.get("denotation")
        if not isinstance(category, str) or category not in keys:
            continue
        if not isinstance(denotation, str) or not denotation.strip():
            continue
        out[category] = denotation
    return out
