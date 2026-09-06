"""Select and render effective lorebook entries for prompts."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from ..core import Macros

LOREBOOK_SCAN_DEPTH = 6
# The agentic fallback scan only looks at the current turn (previous assistant
# message + current user message), since the Director already saw the history.
AGENTIC_LOREBOOK_SCAN_DEPTH = 2

# The heading Agent-managed lore renders under, in every block. Kept distinct
# from the authored heading so the model (and the user reading a prompt dump)
# can always tell established lore from state the roleplay itself produced.
DYNAMIC_SECTION_TITLE = "Dynamic World State"


def is_dynamic(entry: Mapping[str, Any]) -> bool:
    """True when *entry* is an Agent-managed overlay row rather than authored lore."""
    return entry.get("entry_layer") == "dynamic"


def select_effective_entries(entries: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Resolve the authored + dynamic pool into the lore actually in effect.

    A disabled or archived row is inert, so disabling an overlay re-exposes the
    authored entry it was hiding. Returns entries in input order (authored and dynamic interleaved as they
    came); ordering into sections is :func:`render_lorebook_block`'s job. Rows
    without the overlay columns — a hand-built dict in a test, a pre-migration
    row — read as authored, so this is a no-op on a World that has never used
    the feature.
    """
    live = [e for e in entries if bool(e.get("enabled", 1)) and not e.get("archived")]
    hidden: set[int] = set()
    for e in live:
        if is_dynamic(e) and e.get("overlay_action") in ("replace", "suppress"):
            target = e.get("supersedes_entry_id")
            if target is not None:
                hidden.add(int(target))
    return [e for e in live if not (is_dynamic(e) and e.get("overlay_action") == "suppress") and e.get("id") not in hidden]


def build_lorebook_catalog(entries: Sequence[Mapping[str, Any]]) -> str:
    """Build the Director's lorebook catalog for the agentic activation path.

    Lists each non-``constant`` entry (name + up to 3 keywords, excluding any
    keyword equal to the entry name), grouped by
    world. Constant entries are always injected and excluded here. Returns
    ``""`` when there are no non-constant candidates.

    Projected first, so the Director is offered the lore that is actually in
    effect: a replaced authored entry never appears alongside the dynamic entry
    that supersedes it, and a suppressed one is not offered at all.
    """
    candidates = [e for e in select_effective_entries(entries) if not e.get("constant")]
    if not candidates:
        return ""

    groups: dict[str, list[Mapping[str, Any]]] = {}
    for e in candidates:
        groups.setdefault(e.get("world_name") or "", []).append(e)

    parts = [
        "**Available Lorebook Entries** — activate the ones relevant to the scene via `selected_lorebook_entries`. Possible values are wrapped in square brackets."
    ]
    for world, items in groups.items():
        if world:
            parts.append(f"### {world}")
        for e in items:
            name = e.get("name", "")
            name_fold = name.casefold()
            keywords = [kw for kw in (e.get("keywords", []) or []) if kw.casefold() != name_fold]
            kws = ", ".join(keywords[:3])
            parts.append(f"- [{name}] — {kws}" if kws else f"- [{name}]")
    return "\n".join(parts)


def _any_keyword_hit(
    keywords: Sequence[str],
    scan_text: str,
    lowered: str,
    *,
    use_regex: bool,
    case_insensitive: bool,
) -> bool:
    """True when any of *keywords* hits *scan_text* (substring, or regex under V3's ``use_regex``)."""
    flags = re.IGNORECASE if case_insensitive else 0
    for kw in keywords:
        if use_regex:
            # `re` caches compiled patterns internally — no manual cache
            # for a ~55-entry scan. A literal key that happens to be valid regex
            # (`C++`, `(H)`) is *reinterpreted*, which is what a spec-compliant
            # reader does; the except only nets syntactically invalid patterns
            # (`*star`), which degrade to the substring path below.
            try:
                if re.search(kw, scan_text, flags):
                    return True
                continue
            except re.error:
                pass
        if (kw.lower() if case_insensitive else kw) in (lowered if case_insensitive else scan_text):
            return True
    return False


def select_keyword_entries(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    scan_depth: int = LOREBOOK_SCAN_DEPTH,
) -> list[Mapping[str, Any]]:
    """Select lorebook entries by keyword scan.

    A pure keyword source: entries are selected when any keyword appears
    (substring match, or a regex match when the entry sets ``use_regex``) in the
    ``scan_depth`` most recent messages. A ``selective`` entry additionally
    requires one of its ``secondary_keys`` to hit. Constant entries are not this
    source's concern — they ride the system prefix. Returns matched entries in
    input order.
    """
    scan_parts = [m.get("content") or "" for m in messages[-scan_depth:] if m.get("content")]
    scan_text = " ".join(scan_parts)
    lowered = scan_text.lower()
    matched: list[Mapping[str, Any]] = []

    for entry in entries:
        keywords = entry.get("keywords", [])
        if not keywords or not scan_text:
            continue

        rx = bool(entry.get("use_regex"))
        ci = bool(entry.get("case_insensitive", True))
        if not _any_keyword_hit(keywords, scan_text, lowered, use_regex=rx, case_insensitive=ci):
            continue

        # The importer only sets `selective` when secondary_keys is non-empty
        # (blanket-selective cards would otherwise match nothing), so the
        # emptiness check here is belt-and-braces for hand-edited rows.
        secondary = entry.get("secondary_keys") or []
        if (
            entry.get("selective")
            and secondary
            and not _any_keyword_hit(secondary, scan_text, lowered, use_regex=rx, case_insensitive=ci)
        ):
            continue

        matched.append(entry)

    return matched


def _unwrap_catalog_delimiters(text: str) -> str | None:
    """The inside of *text*'s outer ``[...]`` pair, or ``None`` when it has none.

    "Outer pair" is literal: the leading ``[`` must be closed by the *final*
    ``]``. ``[A] and [B]`` therefore unwraps to nothing — its first bracket
    closes early, so the string is two delimited names rather than one wrapped
    one — while ``[Name [with] brackets]`` unwraps to its inner text.
    """
    if len(text) < 2 or not (text.startswith("[") and text.endswith("]")):
        return None
    depth = 0
    for i, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0 and i != len(text) - 1:
                return None
    return text[1:-1] if depth == 0 else None


def normalize_director_pick(pick: str) -> str:
    """Normalize one Director lorebook pick."""
    trimmed = (pick or "").strip()
    inner = _unwrap_catalog_delimiters(trimmed)
    return (inner.strip() if inner is not None else trimmed).casefold()


def _fold_name(entry: Mapping[str, Any]) -> str:
    """The match key for an entry's own ``name``."""
    return (entry.get("name", "") or "").strip().casefold()


def _resolve_director_picks(
    picks: Sequence[str],
    selectable: set[str],
    known: set[str],
) -> tuple[set[str], list[str], list[str]]:
    """Return matched, delimiter-recovered, and unknown Director picks.

    Two numbers fall out of this and both matter. A pick counted as *recovered*
    would have activated nothing before delimiter stripping, so its rate is the
    per-model rate of the catalog-delimiter defect and the signal that says
    whether the prompt also needs work. A pick that names nothing at all is
    either a hallucinated entry or a whole catalog row copied in place of a
    name. Picks naming a *constant* entry are neither: those are excluded from
    the trailing block on purpose, so they stay silent.
    """
    matched: set[str] = set()
    recovered: list[str] = []
    unmatched: list[str] = []
    for pick in picks:
        raw = (pick or "").strip()
        key = normalize_director_pick(raw)
        if key in selectable:
            matched.add(key)
            if key != raw.casefold():
                recovered.append(raw)
        elif key not in known:
            unmatched.append(raw)
    return matched, recovered, unmatched


def director_pick_diagnostics(
    entries: Sequence[Mapping[str, Any]],
    picks: Sequence[str],
) -> tuple[list[str], list[str]]:
    """Return delimiter-recovered and unknown picks for upper-layer logging."""
    effective = select_effective_entries(entries)
    candidates = [entry for entry in effective if not entry.get("constant")]
    _, recovered, unmatched = _resolve_director_picks(
        picks,
        {_fold_name(entry) for entry in candidates},
        {_fold_name(entry) for entry in effective},
    )
    return recovered, unmatched


def select_active_entries(
    entries: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    scan_depth: int,
    director_selected: Sequence[str] = (),
) -> list[Mapping[str, Any]]:
    """Select the active lorebook entries for the trailing block.

    An entry is active when a keyword matched within the ``scan_depth`` most
    recent messages, OR its ``name`` is in *director_selected* (case-insensitive,
    trimmed, and stripped of the catalog's own ``[...]`` delimiters — see
    :func:`normalize_director_pick`). The pool is projected to the effective
    layer first, then constant entries are excluded — they ride the cached
    system prefix, and filtering here (rather than per caller) also keeps a
    director pick that names a constant entry from duplicating it into the
    trailing block. Returns entries in input order — the union underlying both
    the substring (``director_selected=()``) and agentic paths.
    """
    effective = select_effective_entries(entries)
    candidates = [e for e in effective if not e.get("constant")]
    director_named, _, _ = _resolve_director_picks(
        director_selected,
        {_fold_name(e) for e in candidates},
        {_fold_name(e) for e in effective},
    )
    keyword_hit = {id(e) for e in select_keyword_entries(messages or [], candidates, scan_depth)}

    def is_active(entry: Mapping[str, Any]) -> bool:
        return id(entry) in keyword_hit or _fold_name(entry) in director_named

    return [e for e in candidates if is_active(e)]


def _render_section(entries: Sequence[Mapping[str, Any]], resolve, header: str) -> list[str]:
    """Render one layer's entries under *header*, or nothing when it is empty."""
    if not entries:
        return []
    matched = sorted(entries, key=lambda e: (-e.get("priority", 100), e.get("sort_order", 0), e.get("id", 0)))
    parts = [header]
    for entry in matched:
        name = resolve(entry.get("name", ""))
        content = resolve(entry.get("content", ""))
        if name and content:
            parts.append(f"{name}: {content}")
        elif content:
            parts.append(content)
    # A section whose every entry rendered blank contributes no header either.
    return parts if len(parts) > 1 else []


def render_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
    *,
    header: str = "**Lorebook**",
    dynamic_header: str = f"**{DYNAMIC_SECTION_TITLE}**",
) -> str:
    """Render already-selected lorebook entries into a *header*-titled block.

    The single rendering point for every activation path. Authored entries come
    first under *header*; Agent-managed ones follow under *dynamic_header*, so
    the model reads established lore before the state the roleplay has since
    produced. Within each section entries are sorted by priority DESC, then
    sort_order ASC, id ASC (the canonical lorebook order, so the block bytes are
    stable across turns regardless of input order — KV cache); names and content
    are macro-resolved. Returns ``""`` when *entries* is empty.
    """
    if not entries:
        return ""

    resolve = macros.resolve_message if macros else (lambda t: t)
    parts = _render_section([e for e in entries if not is_dynamic(e)], resolve, header)
    parts += _render_section([e for e in entries if is_dynamic(e)], resolve, dynamic_header)
    return "\n\n".join(parts)


def compute_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    messages: Sequence[Mapping[str, Any]] | None,
    *,
    scan_depth: int,
    director_selected: Sequence[str] = (),
    macros: Macros | None = None,
) -> str:
    """Select active entries (all sources) and render the ``**Lorebook**`` block.

    The shared core behind both named entry points and the pipeline's
    ``LorebookTurn.writer_block`` (``pipeline/state.py``).
    """
    return render_lorebook_block(
        select_active_entries(entries, messages, scan_depth=scan_depth, director_selected=director_selected),
        macros,
    )


def compute_lorebook_injection_block(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Substring path: build the trailing lorebook block by keyword scanning.

    Entries are included when a keyword matches within the 6 most recent
    messages. Constant entries are excluded — they ride the cached system prefix
    (:func:`compute_constant_lorebook_block`) or, with ``at_depth``, the depth
    block (:func:`compute_depth_lorebook_block`). Sorted by priority DESC.
    Returns ``""`` when nothing matches.
    """
    return compute_lorebook_block(entries, messages, scan_depth=LOREBOOK_SCAN_DEPTH, macros=macros)


def compute_agentic_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    selected_names: Sequence[str],
    macros: Macros | None = None,
    messages: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    """Agentic path: build the trailing lorebook block from the Director's selection.

    Includes entries whose ``name`` matches *selected_names* (case-insensitive,
    trimmed) + entries triggered by a keyword scan over the current turn
    (``AGENTIC_LOREBOOK_SCAN_DEPTH``), so keywords the Director overlooks still
    activate their entries. Constant entries are excluded — they ride the cached
    system prefix (:func:`compute_constant_lorebook_block`) or, with
    ``at_depth``, the depth block (:func:`compute_depth_lorebook_block`).
    Returns ``""`` when nothing matches.
    """
    return compute_lorebook_block(
        entries,
        messages or [],
        scan_depth=AGENTIC_LOREBOOK_SCAN_DEPTH,
        director_selected=selected_names or (),
        macros=macros,
    )


def compute_constant_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Render constant lorebook entries for the prompt prefix."""
    prefix_bound = [e for e in select_effective_entries(entries) if e.get("constant") and not e.get("at_depth")]
    return render_lorebook_block(prefix_bound, macros, header="## Lorebook", dynamic_header=f"## {DYNAMIC_SECTION_TITLE}")


def compute_depth_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
) -> str:
    """Render depth-positioned lorebook entries for the prompt tail."""
    depth_bound = [e for e in select_effective_entries(entries) if e.get("constant") and e.get("at_depth")]
    fresh = macros._replace(seed="") if macros else None
    return render_lorebook_block(
        depth_bound, fresh, header="**Lorebook (Depth)**", dynamic_header=f"**{DYNAMIC_SECTION_TITLE} (Depth)**"
    )
