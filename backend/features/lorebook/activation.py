"""
activation.py — Lorebook activation: one pipeline, three sources.

A lorebook entry activates from any of three sources:
  * ``constant`` — always active; rides the cached system-prompt prefix
    (:func:`compute_constant_lorebook_block`), never the trailing block.
  * keyword scan — a keyword appears (substring) within the last ``scan_depth`` messages.
  * director pick — the agentic Director named the entry.

The trailing block carries only the per-turn sources: substring mode uses
{keyword@6}; agentic mode adds the director-pick source and scans shallower
({keyword@2, director-pick}) since the Director already saw the history. Both
modes funnel through :func:`select_active_entries` → :func:`render_lorebook_block`,
so the two named entry points below (:func:`compute_lorebook_injection_block`,
:func:`compute_agentic_lorebook_block`) are thin wrappers over one core.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ...core import Macros

LOREBOOK_SCAN_DEPTH = 6
# The agentic fallback scan only looks at the current turn (previous assistant
# message + current user message), since the Director already saw the history.
AGENTIC_LOREBOOK_SCAN_DEPTH = 2


# ── Activation gating ─────────────────────────────────────────────────────────


def agentic_lorebook_active(
    settings: Mapping[str, Any],
    lorebook_entries: Sequence[Mapping[str, Any]],
    *,
    agent_on: bool,
) -> bool:
    """Return True when the director should pick lorebook entries this turn.

    Requires the feature flag, the global agent on, and at least one non-constant
    entry. It is independent of ``direct_scene``: the picks run in their own
    forced ``select_lorebook`` call, so agentic lorebook works whether or not the
    Director's scene-direction tool is enabled. Constant entries are always
    injected and never managed by the director, so a pool of only constants does
    not enable agentic mode.

    *agent_on* is passed in (rather than recomputed) so ``agent_enabled`` stays
    the single source of truth — mirroring ``resolve_length_guard``.
    """
    if not bool(settings.get("agentic_lorebook_enabled", 0)):
        return False
    if not agent_on:
        return False
    return any(not e.get("constant") for e in lorebook_entries)


# ── Director-facing catalog (agentic mode) ────────────────────────────────────


def build_lorebook_catalog(entries: Sequence[Mapping[str, Any]]) -> str:
    """Build the Director's lorebook catalog for the agentic activation path.

    Lists each non-``constant`` entry (name + up to 3 keywords, excluding any
    keyword equal to the entry name), grouped by
    world. Constant entries are always injected and excluded here. Returns
    ``""`` when there are no non-constant candidates.
    """
    candidates = [e for e in entries if not e.get("constant")]
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


# ── Activation sources + selection ────────────────────────────────────────────


def select_keyword_entries(
    messages: Sequence[Mapping[str, Any]],
    entries: Sequence[Mapping[str, Any]],
    scan_depth: int = LOREBOOK_SCAN_DEPTH,
) -> list[Mapping[str, Any]]:
    """Select lorebook entries by keyword/substring scan.

    A pure keyword source: entries are selected when any keyword appears
    (substring match) in the ``scan_depth`` most recent messages. Constant
    entries are not this source's concern — they ride the system prefix.
    Returns matched entries in input order.
    """
    scan_parts = [m.get("content") or "" for m in messages[-scan_depth:] if m.get("content")]
    scan_text = " ".join(scan_parts)
    matched: list[Mapping[str, Any]] = []

    for entry in entries:
        keywords = entry.get("keywords", [])
        if not keywords or not scan_text:
            continue

        case_insensitive = entry.get("case_insensitive", True)
        text = scan_text.lower() if case_insensitive else scan_text

        found = False
        for kw in keywords:
            kw_text = kw.lower() if case_insensitive else kw
            if kw_text in text:
                found = True
                break

        if found:
            matched.append(entry)

    return matched


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
    trimmed). Constant entries are excluded up front — they ride the cached
    system prefix, and filtering here (rather than per caller) also keeps a
    director pick that names a constant entry from duplicating it into the
    trailing block. Returns entries in input order — the union underlying both
    the substring (``director_selected=()``) and agentic paths.
    """
    entries = [e for e in entries if not e.get("constant")]
    director_named = {(n or "").strip().casefold() for n in director_selected}
    keyword_hit = {id(e) for e in select_keyword_entries(messages or [], entries, scan_depth)}

    def is_active(entry: Mapping[str, Any]) -> bool:
        name = (entry.get("name", "") or "").strip().casefold()
        return id(entry) in keyword_hit or name in director_named

    return [e for e in entries if is_active(e)]


# ── Rendering ─────────────────────────────────────────────────────────────────


def render_lorebook_block(
    entries: Sequence[Mapping[str, Any]],
    macros: Macros | None = None,
    *,
    header: str = "**Lorebook**",
) -> str:
    """Render already-selected lorebook entries into a *header*-titled block.

    The single rendering point for every activation path. Entries are sorted by
    priority DESC, then sort_order ASC, id ASC (the canonical lorebook order, so
    the block bytes are stable across turns regardless of input order — KV cache);
    names and content are macro-resolved. Returns ``""`` when *entries* is empty.
    """
    if not entries:
        return ""

    matched = sorted(entries, key=lambda e: (-e.get("priority", 100), e.get("sort_order", 0), e.get("id", 0)))

    resolve = macros.resolve_message if macros else (lambda t: t)
    parts = [header]
    for entry in matched:
        name = resolve(entry.get("name", ""))
        content = resolve(entry.get("content", ""))
        if name and content:
            parts.append(f"{name}: {content}")
        elif content:
            parts.append(content)

    return "\n\n".join(parts)


# ── Block builders ────────────────────────────────────────────────────────────


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
    messages. Constant entries are excluded — they ride the cached system
    prefix (:func:`compute_constant_lorebook_block`). Sorted by priority DESC.
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
    system prefix (:func:`compute_constant_lorebook_block`). Returns ``""`` when
    nothing matches.
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
    """Prefix path: render the ``constant`` entries as a system-prompt section.

    Constant entries are byte-identical every turn, so they live in the cached
    system-prompt prefix (``## Lorebook``, matching the prefix's ``##`` section
    register) instead of the per-turn trailing block. The canonical sort in
    :func:`render_lorebook_block` keeps the bytes stable across turns (KV cache);
    like other prefix fields, entry text should avoid ``{{roll}}`` — it re-rolls
    per resolution and would silently change prefix bytes. Returns ``""`` when
    there are no constant entries.
    """
    return render_lorebook_block([e for e in entries if e.get("constant")], macros, header="## Lorebook")
