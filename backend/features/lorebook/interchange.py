"""Lorebook views and Character Card / World Info book conversion.

Pure translation between a World's stored entries and the ``character_book``
shape cards and standalone World Info files carry, in both directions.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ...prompting.lorebook import select_effective_entries


def project_lorebook_view(entries: Sequence[Mapping[str, Any]], view: str) -> list[Mapping[str, Any]]:
    """Project a World into the requested lorebook view."""
    if view == "authored":
        return [e for e in entries if e.get("entry_layer") != "dynamic"]
    return list(select_effective_entries(entries)) if view == "effective" else list(entries)


# A V3 entry may open with decorator lines (`@@depth 4`, `@@@fallback`, …).
_DECORATOR_PREAMBLE = re.compile(r"\A\s*(?:@@[^\n]*\n?)+")


def _strip_decorators(content: str) -> str:
    """Drop the V3 decorator preamble from an entry's content."""
    stripped = _DECORATOR_PREAMBLE.sub("", content)
    return stripped.lstrip("\n") if stripped != content else content


def _str_list(value: Any) -> list[str]:
    return [str(k) for k in value if k] if isinstance(value, list) else []


def normalise_lorebook_entry(item: dict) -> dict:
    keywords = _str_list(item.get("keys") or item.get("key") or [])
    secondary_keys = _str_list(item.get("secondary_keys") or item.get("keysecondary") or [])
    name = item.get("name") or item.get("comment") or ""
    if "disable" in item:
        enabled = not item["disable"]
    else:
        enabled = bool(item.get("enabled", True))
    priority = int(item.get("priority") or item.get("insertion_order") or item.get("order") or 100)
    # A standalone World Info file keeps its non-V2 entry fields at the top
    # level; a card-embedded `character_book` parks the same fields under
    # `extensions` (position, depth, case_sensitive, …). Read both spellings
    # so either export lands intact.
    raw_ext = item.get("extensions")
    ext: dict = raw_ext if isinstance(raw_ext, dict) else {}
    case_sensitive = item.get("caseSensitive") or item.get("case_sensitive") or ext.get("case_sensitive")
    constant = bool(item.get("constant", False))
    return {
        "name": str(name),
        "content": _strip_decorators(str(item.get("content") or "")),
        "keywords": keywords,
        "enabled": enabled,
        "priority": priority,
        # `priority` keeps its own fallback chain above (rewriting it would
        # reshuffle already-imported V2 books); sort_order carries the spec field.
        "sort_order": int(item.get("insertion_order") or 0),
        "case_insensitive": not bool(case_sensitive),
        "constant": constant,
        # World Info's `position: 4` is "@ Depth" — injected after the latest
        # message instead of into the character defs. V2/V3 `character_book`
        # spells the top-level position as a string ("before_char"/"after_char"),
        # which is never 4; the numeric one lives in `extensions`. `at_depth` is
        # our own export key, read back so an Orb round-trip is lossless.
        "at_depth": bool(item.get("at_depth")) or 4 in (item.get("position"), ext.get("position")),
        "use_regex": bool(item.get("use_regex", False)),
        # Cards in the wild set `selective` on every entry while leaving
        # secondary_keys empty; honouring that literally would make the whole
        # book match nothing, so an unbacked flag stores as false.
        "selective": bool(item.get("selective")) and bool(secondary_keys),
        "secondary_keys": secondary_keys,
    }


def lorebook_to_book(
    world_name: str,
    entries: Sequence[Mapping[str, Any]],
    *,
    dynamic_enabled: bool = False,
) -> dict[str, Any]:
    """Serialize a World lorebook to Character Card shape."""
    return {
        "name": world_name,
        # Orb's own marker. It round-trips the Dynamic World flag, and its mere
        # presence tells the importer the book is a World Orb exported — so an
        # entry-less one is a real lorebook to restore (a Dynamic World starts
        # empty by design) rather than the vestigial `entries: []` that foreign
        # cards carry.
        "extensions": {"orb": {"dynamic_enabled": bool(dynamic_enabled)}},
        "entries": [
            {
                "keys": e["keywords"],
                "content": e["content"],
                # World Info readers take placement and case-sensitivity from
                # here, not from the V2 top-level keys — without this block a
                # round-trip drops @ Depth and the case flag. `depth: 0` is where
                # Orb puts the block: immediately after the latest message.
                "extensions": {
                    "position": 4 if e.get("at_depth") else 1,
                    "depth": 0,
                    "case_sensitive": not bool(e["case_insensitive"]),
                },
                "position": "after_char",
                "enabled": bool(e["enabled"]),
                "insertion_order": e["sort_order"],
                "case_sensitive": not bool(e["case_insensitive"]),
                "constant": bool(e.get("constant", False)),
                # Additive: our own spelling of the depth flag, read back on import.
                "at_depth": bool(e.get("at_depth", False)),
                "name": e["name"],
                # World Info readers title an entry from `comment`; `name` is the
                # V2 spelling. Both, so either reader shows the title.
                "comment": e["name"],
                "priority": e["priority"],
                "id": e["id"],
                "use_regex": bool(e.get("use_regex", False)),
                "selective": bool(e.get("selective", False)),
                "secondary_keys": e.get("secondary_keys") or [],
            }
            for e in entries
        ],
    }
